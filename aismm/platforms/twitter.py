"""X (Twitter) — API v2.

Posting text uses ``POST /2/tweets`` with an OAuth 2.0 (PKCE) user token.
Media (image/video) uses the chunked upload flow (INIT / APPEND / FINALIZE →
optional STATUS poll) and requires the ``media.write`` scope; the returned
``media_id`` is attached to the tweet.

Scopes: ``tweet.read tweet.write users.read media.write offline.access``
(``offline.access`` yields a refresh token). Media upload historically also lived
on the v1.1 endpoint under OAuth 1.0a — if the v2 upload 403s for your app, add
your OAuth1.0a consumer keys (TWITTER_API_KEY/SECRET) and switch ``_upload_media``
to the v1.1 flow (see README).
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

from ..assets import read_bytes
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

logger = logging.getLogger("aismm.platforms.twitter")

API = "https://api.x.com/2"
_CHUNK = 4 * 1024 * 1024  # <5MB per APPEND


class Twitter(SocialPlatform):
    name = PlatformName.twitter
    capabilities = Capabilities(
        supports_text=True,
        supports_image=True,
        supports_video=True,
        needs_public_media_url=False,
        default_orientation="landscape",
        caption_limit=280,
        notes="280 chars. Up to 4 images OR 1 video per post. "
              "Media via chunked upload (media.write scope).",
        # X allows four images in one post — the same "carousel" idea, so the
        # publish tool's item-count check covers it without special-casing.
        supports_carousel=True,
        max_carousel_items=4,
    )
    auth_endpoint = "https://x.com/i/oauth2/authorize"
    token_endpoint = f"{API}/oauth2/token"
    scopes = ["tweet.read", "tweet.write", "users.read", "media.write", "offline.access"]
    use_pkce = True
    token_auth_style = "basic"

    async def fetch_identity(self, access_token: str) -> Identity:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{API}/users/me",
                                 headers={"Authorization": f"Bearer {access_token}"})
            r.raise_for_status()
            data = r.json().get("data", {})
        return Identity(external_id=data.get("id", ""), handle=data.get("username", ""))

    async def _upload_media(self, client, access_token, path, media_kind) -> str:
        media_type = "video/mp4" if media_kind == "video" else "image/jpeg"
        category = "tweet_video" if media_kind == "video" else "tweet_image"
        data = read_bytes(path)   # blob-aware: falls back to Azure when local is gone
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"{API}/media/upload"

        # INIT
        r = await client.post(url, headers=headers, data={
            "command": "INIT", "total_bytes": str(len(data)),
            "media_type": media_type, "media_category": category})
        r.raise_for_status()
        media_id = str(r.json()["data"]["id"] if "data" in r.json() else r.json()["media_id"])

        # APPEND (chunked)
        for idx, start in enumerate(range(0, len(data), _CHUNK)):
            chunk = data[start:start + _CHUNK]
            r = await client.post(url, headers=headers,
                                  data={"command": "APPEND", "media_id": media_id, "segment_index": str(idx)},
                                  files={"media": ("chunk", chunk, "application/octet-stream")})
            r.raise_for_status()

        # FINALIZE
        r = await client.post(url, headers=headers, data={"command": "FINALIZE", "media_id": media_id})
        r.raise_for_status()
        info = r.json().get("data", r.json())
        processing = info.get("processing_info")

        # STATUS poll (videos)
        while processing and processing.get("state") in {"pending", "in_progress"}:
            await asyncio.sleep(processing.get("check_after_secs", 3))
            r = await client.get(url, headers=headers,
                                 params={"command": "STATUS", "media_id": media_id})
            r.raise_for_status()
            processing = r.json().get("data", r.json()).get("processing_info")
        if processing and processing.get("state") == "failed":
            raise RuntimeError(f"X media processing failed: {processing.get('error')}")
        return media_id

    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind,
                      instruction=None, asset_paths=None, placement="feed") -> PublishResult:
        """Post a tweet, with up to four images (X's limit) or one video.

        ``asset_paths`` and ``placement`` are part of the :class:`SocialPlatform`
        contract — ``perform_publish`` always passes them. Omitting them here is
        what made the first ever X publish die with ``got an unexpected keyword
        argument 'asset_paths'`` after the agent had already generated the image.
        """
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        payload: dict = {"text": caption[: self.capabilities.caption_limit]}
        paths = [p for p in (asset_paths or [asset_path]) if p]
        # One video, or up to four images — X refuses a mixed set.
        if media_kind == "video":
            paths = paths[:1]
        else:
            paths = paths[: self.capabilities.max_carousel_items]

        async with httpx.AsyncClient(timeout=120) as client:
            # No os.path.exists gate: the bytes may live in blob storage.
            if media_kind in {"image", "video"} and paths:
                media_ids = [await self._upload_media(client, access_token, path, media_kind)
                             for path in paths]
                payload["media"] = {"media_ids": media_ids}
            r = await client.post(f"{API}/tweets", headers=headers, json=payload)
            r.raise_for_status()
            data = r.json().get("data", {})
        tweet_id = data.get("id", "")
        handle = account.handle or "i"
        return PublishResult(url=f"https://x.com/{handle}/status/{tweet_id}" if tweet_id else "",
                             external_id=tweet_id, raw=data)


    # ------------------------------------------------------------------ #
    # Reading and engagement
    #
    # NOTE ON ACCESS TIERS: X's Free tier is essentially write-only (posting and
    # deleting). Every READ here — timeline, mentions, metrics — needs at least
    # Basic, and returns 403 on Free. The tools surface that verbatim rather than
    # pretending the account is empty, because "no posts" and "your plan cannot
    # read posts" call for very different responses from the agent.
    # ------------------------------------------------------------------ #
    TWEET_FIELDS = "id,text,created_at,public_metrics,conversation_id,referenced_tweets"

    @staticmethod
    def _api_error(response: httpx.Response) -> RuntimeError:
        """X errors are JSON; surface the reason instead of a bare status code."""
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = {}
        detail = (body.get("detail") or body.get("title")
                  or (body.get("errors") or [{}])[0].get("message")
                  or (response.text or "")[:300])
        hint = ""
        if response.status_code in (401, 403):
            hint = (" — X's Free tier is write-only; reading posts, mentions or metrics "
                    "needs at least the Basic plan.")
        return RuntimeError(f"X API {response.status_code}: {detail}{hint}")

    async def _get(self, access_token: str, path: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{API}/{path}", params=params,
                                 headers={"Authorization": f"Bearer {access_token}"})
            if r.status_code >= 400:
                raise self._api_error(r)
            return r.json()

    async def list_posts(self, access_token: str, account: Account, *,
                         limit: int = 10) -> list[dict]:
        """This account's own recent posts — so the agent doesn't repeat itself."""
        payload = await self._get(access_token, f"users/{account.external_id}/tweets", {
            "max_results": max(5, min(limit, 100)), "tweet.fields": self.TWEET_FIELDS})
        return payload.get("data", []) or []

    async def list_mentions(self, access_token: str, account: Account, *,
                            limit: int = 10) -> list[dict]:
        """Posts that mentioned this account — the other half of engagement."""
        payload = await self._get(access_token, f"users/{account.external_id}/mentions", {
            "max_results": max(5, min(limit, 100)), "tweet.fields": self.TWEET_FIELDS})
        return payload.get("data", []) or []

    async def post_metrics(self, access_token: str, post_id: str) -> dict:
        """Impressions/likes/reposts for one post."""
        payload = await self._get(access_token, f"tweets/{post_id}",
                                  {"tweet.fields": self.TWEET_FIELDS})
        return payload.get("data", {}) or {}

    async def profile(self, access_token: str) -> dict:
        """Follower and post counts for the connected account."""
        payload = await self._get(access_token, "users/me", {
            "user.fields": "id,name,username,description,public_metrics,verified"})
        return payload.get("data", {}) or {}

    async def reply(self, access_token: str, post_id: str, text: str) -> dict:
        """Reply to a post, in the account's voice. Posts IMMEDIATELY."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{API}/tweets",
                headers={"Authorization": f"Bearer {access_token}",
                         "Content-Type": "application/json"},
                json={"text": text[: self.capabilities.caption_limit],
                      "reply": {"in_reply_to_tweet_id": post_id}})
            if r.status_code >= 400:
                raise self._api_error(r)
            return r.json().get("data", {})

    async def delete_post(self, access_token: str, post_id: str) -> dict:
        """Delete one of this account's own posts."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.delete(f"{API}/tweets/{post_id}",
                                    headers={"Authorization": f"Bearer {access_token}"})
            if r.status_code >= 400:
                raise self._api_error(r)
            return r.json().get("data", {})

    async def post_exists(self, access_token: str, account: Account,
                          external_id: str) -> bool | None:
        """Is this post still up? Used by the duplicate guard before it refuses.

        ``None`` when it cannot tell — including the Free tier's 403, where
        "cannot read" must not be mistaken for "was deleted".
        """
        if not external_id:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{API}/tweets/{external_id}", params={"ids": external_id},
                                     headers={"Authorization": f"Bearer {access_token}"})
            if r.status_code == 404:
                return False
            if r.status_code >= 400:
                return None
            body = r.json()
            if body.get("data"):
                return True
            errors = body.get("errors") or []
            # X reports a deleted post as a 200 with an errors[] entry.
            if any((e.get("title") or "").lower().startswith("not found") for e in errors):
                return False
            return None
        except Exception as exc:  # noqa: BLE001 - diagnostics never break a publish
            logger.warning("X post lookup failed for %s: %s", external_id, exc)
            return None


register(PlatformName.twitter, Twitter)
