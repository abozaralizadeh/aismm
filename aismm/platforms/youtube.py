"""YouTube — Data API v3 (``videos.insert``, resumable upload).

Video-only. OAuth 2.0 with the ``youtube.upload`` scope (+ ``youtube.readonly`` to
resolve the channel). ``access_type=offline`` + ``prompt=consent`` yields a refresh
token. Each upload costs ~1600 quota units.

Resumable flow:
    1. POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status
       (JSON metadata; headers X-Upload-Content-Type/-Length) -> Location header = upload URI
    2. PUT the bytes to that Location URI -> video resource {id}

The caption's first line becomes the title; the remainder becomes the description.
"""
from __future__ import annotations

import logging

import httpx

from .. import disclosure
from ..assets import read_bytes
from ..config import YOUTUBE_PRIVACY_CHOICES, settings
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

logger = logging.getLogger("aismm.platforms.youtube")

def resolve_privacy(instruction=None) -> str:
    """Visibility for this upload: the instruction's choice, else the deployment's.

    Read through ``settings`` rather than ``os.getenv`` — config is a frozen
    singleton built at import, and a stray getenv is invisible to the tests that
    pin the environment. An unrecognised value falls back to the default rather
    than being sent to YouTube, which rejects it with a generic 400.
    """
    chosen = str(getattr(instruction, "youtube_privacy", "") or "").strip().lower()
    if chosen in YOUTUBE_PRIVACY_CHOICES:
        return chosen
    if chosen:
        logger.warning("Ignoring unknown YouTube privacy %r; using %s",
                       chosen, settings.youtube_privacy)
    return settings.youtube_privacy


UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
DATA_API = "https://www.googleapis.com/youtube/v3"


class YouTube(SocialPlatform):
    name = PlatformName.youtube
    capabilities = Capabilities(
        supports_text=False,
        supports_image=False,
        supports_video=True,
        needs_public_media_url=False,
        default_orientation="portrait",   # Shorts (9:16, <=60s); use landscape for long-form
        caption_limit=5000,               # description limit; title capped at 100
        notes="Video only. Title = first line of caption (<=100 chars); rest = description.",
        # Comment threads on the channel's videos are readable and answerable via
        # the Data API (needs the youtube.force-ssl scope — reconnect to grant it).
        supports_comments=True,
        # videos.list?part=statistics returns view/like/comment counts per video —
        # the feedback loop reads them (youtube.readonly, already requested).
        supports_metrics=True,
    )
    auth_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    scopes = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
        # Reading comment threads and inserting replies both require force-ssl;
        # an account connected before this was added must be reconnected.
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]
    extra_authorize_params = {"access_type": "offline", "prompt": "consent"}

    async def fetch_identity(self, access_token: str) -> Identity:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(CHANNELS_URL,
                                 params={"part": "snippet", "mine": "true"},
                                 headers={"Authorization": f"Bearer {access_token}"})
            r.raise_for_status()
            items = r.json().get("items", [])
        if not items:
            raise RuntimeError("No YouTube channel found for this Google account.")
        ch = items[0]
        return Identity(external_id=ch["id"], handle=ch["snippet"].get("title", ""))

    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind,
                      instruction=None, asset_paths=None, placement="feed") -> PublishResult:
        """Upload one video.

        ``asset_paths`` / ``placement`` are part of the :class:`SocialPlatform`
        contract and are always passed; YouTube is single-video, so the extra
        paths are refused rather than silently dropped. ``Capabilities`` already
        declares no carousel, so `perform_publish` normally catches this first.
        """
        if asset_paths and len(asset_paths) > 1:
            raise RuntimeError(
                f"YouTube takes one video per upload; {len(asset_paths)} assets were passed. "
                f"Publish them as separate videos.")
        asset_path = asset_path or (asset_paths[0] if asset_paths else "")
        if media_kind != "video" or not asset_path:
            raise RuntimeError("YouTube requires a video asset; generate a video first.")
        # Read up front: the bytes may live in blob storage rather than on disk.
        video_bytes = read_bytes(asset_path)
        title, _, description = caption.partition("\n")
        privacy = resolve_privacy(instruction)
        metadata = {
            "snippet": {"title": title[:100] or "Untitled", "description": description.strip()},
            "status": {"privacyStatus": privacy,
                       "selfDeclaredMadeForKids": False,
                       # containsSyntheticMedia drives YouTube's altered/synthetic
                       # content disclosure ("How this content was made").
                       **disclosure.native_flags("youtube", instruction)},
        }
        size = len(video_bytes)
        async with httpx.AsyncClient(timeout=None) as client:
            init = await client.post(
                UPLOAD_URL, json=metadata,
                headers={"Authorization": f"Bearer {access_token}",
                         "Content-Type": "application/json; charset=UTF-8",
                         "X-Upload-Content-Type": "video/*",
                         "X-Upload-Content-Length": str(size)})
            init.raise_for_status()
            location = init.headers.get("Location")
            if not location:
                raise RuntimeError("YouTube resumable init returned no upload Location.")
            put = await client.put(
                    location, content=video_bytes,
                    headers={"Authorization": f"Bearer {access_token}",
                             "Content-Type": "video/*", "Content-Length": str(size)})
            put.raise_for_status()
            data = put.json()
        video_id = data.get("id", "")
        # YouTube can accept the upload and LOCK it private anyway: an API project
        # that has not passed the compliance audit has every upload forced to
        # private, and that cannot be appealed — the video has to be re-uploaded
        # through an audited client. Reporting "published" over a silently private
        # video is the worst outcome, so compare what we asked for with what came
        # back and say so on the run.
        landed = str(((data.get("status") or {}).get("privacyStatus") or "")).lower()
        notice = ""
        if landed and landed != privacy:
            notice = (
                f"YouTube published this as {landed.upper()}, not {privacy}. An API project "
                f"that has not passed YouTube's compliance audit has every upload locked to "
                f"private, and the lock cannot be appealed — the video must be re-uploaded "
                f"through an audited client. Request an audit for this Google Cloud project, "
                f"or set the visibility by hand in YouTube Studio."
            )
            logger.warning("YouTube downgraded %s from %s to %s", video_id, privacy, landed)
        return PublishResult(url=f"https://youtu.be/{video_id}" if video_id else "",
                             external_id=video_id,
                             raw={**data, **({"notice": notice} if notice else {})})

    async def fetch_post_metrics(self, access_token: str, account: Account, *,
                                 external_id: str) -> dict | None:
        """Normalized view/like/comment counts for one video.

        ``videos.list?part=statistics`` returns the counts as STRINGS, so coerce
        them to ints. Returns ``None`` on any failure so one bad video never stops
        the sweep.
        """
        if not external_id:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{DATA_API}/videos",
                                     params={"part": "statistics", "id": external_id},
                                     headers={"Authorization": f"Bearer {access_token}"})
                r.raise_for_status()
                items = r.json().get("items", [])
        except Exception as exc:  # noqa: BLE001 - one bad video must not stop the sweep
            logger.warning("YouTube metrics for %s failed: %s", external_id, exc)
            return None
        if not items:
            return {}
        stats = items[0].get("statistics") or {}

        def _int(key: str) -> int:
            try:
                return int(stats.get(key, 0))
            except (TypeError, ValueError):
                return 0

        return {
            "views": _int("viewCount"),
            "likes": _int("likeCount"),
            "comments": _int("commentCount"),
        }

    # ------------------------------------------------------------------ #
    # Reading and engagement (comment threads)
    # ------------------------------------------------------------------ #
    async def list_comment_threads(self, access_token: str, account: Account, *,
                                   limit: int = 20) -> list[dict]:
        """Recent comment threads across THIS channel's videos, newest first.

        ``allThreadsRelatedToChannelId`` returns top-level comments on every video
        the channel owns in one call — the engagement run reads these and answers
        the ones worth a reply. Each item's ``id`` is the thread's top-level
        comment id, which is the ``parentId`` a reply is inserted under.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{DATA_API}/commentThreads", params={
                "part": "snippet", "allThreadsRelatedToChannelId": account.external_id,
                "maxResults": max(1, min(limit, 100)), "order": "time",
                "textFormat": "plainText"},
                headers={"Authorization": f"Bearer {access_token}"})
            r.raise_for_status()
            items = r.json().get("items", [])
        threads = []
        for item in items:
            top = ((item.get("snippet") or {}).get("topLevelComment") or {})
            snip = top.get("snippet") or {}
            threads.append({
                "id": top.get("id", ""),                       # parentId for a reply
                "thread_id": item.get("id", ""),
                "text": snip.get("textDisplay") or snip.get("textOriginal") or "",
                "from": snip.get("authorDisplayName", ""),
                "at": snip.get("publishedAt", ""),
                "likes": snip.get("likeCount"),
                "video_id": ((item.get("snippet") or {}).get("videoId") or ""),
                "reply_count": (item.get("snippet") or {}).get("totalReplyCount", 0),
            })
        return threads

    async def reply_to_comment(self, access_token: str, parent_id: str, text: str) -> dict:
        """Insert a reply under a top-level comment (``comments.insert``)."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{DATA_API}/comments", params={"part": "snippet"},
                headers={"Authorization": f"Bearer {access_token}",
                         "Content-Type": "application/json"},
                json={"snippet": {"parentId": parent_id, "textOriginal": text}})
            r.raise_for_status()
            return r.json()

    async def reply_to_target(self, access_token: str, account: Account, *,
                              target_type: str, target_id: str, text: str,
                              reply_to: str = "") -> dict:
        """Reply to a comment (the mode-gated engagement path).

        ``target_id`` is a top-level comment id from ``list_comment_threads``. The
        Data API returns no watch-page anchor for a reply, so ``url`` is empty and
        the ledger keys on the id. ``reply_to`` is unused — YouTube has no DM API,
        and for a comment the reply target IS ``target_id``; the keyword is
        accepted only so the shared ``perform_reply`` call never raises TypeError.
        """
        result = await self.reply_to_comment(access_token, target_id, text)
        return {"id": result.get("id", ""), "url": ""}


register(PlatformName.youtube, YouTube)
