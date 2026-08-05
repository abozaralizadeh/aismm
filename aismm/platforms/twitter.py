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

# initialize validates media_type against this list, so a PNG announced as
# image/jpeg is a 400 rather than a re-encode. The bytes are the authority —
# same reasoning as browse_tool.sniff_media: a path extension can lie, and
# nothing upstream guarantees the generator wrote a JPEG.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _media_type(data: bytes, path: str, media_kind: str) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in {"jpg", "jpeg"}:
        return "image/jpeg"
    if ext in {"png", "gif", "webp"}:
        return f"image/{ext}"
    if ext in {"mp4", "mov", "webm"}:
        return "video/mp4" if ext == "mov" else f"video/{ext}"
    return "video/mp4" if media_kind == "video" else "image/jpeg"


def split_thread(text: str, limit: int, max_posts: int, pin_suffix: str = "") -> list[str]:
    """Break a long caption into posts that each fit ``limit``.

    Splits on the largest natural boundary that works — paragraph, then sentence,
    then word — so a thought is never cut mid-word and rarely mid-sentence.
    Numbered ``n/m`` once there is more than one post, which is the X convention
    and tells a reader how much is coming; the counter is inside the limit, not
    added on top of it.

    ``pin_suffix`` is moved from the end of the text to the FIRST post. That is
    for the AI-disclosure label: it is appended to the caption, so on a thread it
    would otherwise land on the last post — invisible to anyone who only sees
    post 1 in their timeline, which is exactly the "first exposure" the label
    exists to cover.

    The text is truncated only if it cannot fit in ``max_posts`` — a bound that
    exists so a runaway caption can't post fifty times.
    """
    text = (text or "").strip()
    if not text:
        return []

    pin = (pin_suffix or "").strip()
    if pin and text.endswith(pin):
        text = text[: -len(pin)].strip()
        if not text:
            return [pin]
    else:
        pin = ""

    if len(text) + (len(pin) + 2 if pin else 0) <= limit:
        return [f"{text}\n\n{pin}" if pin else text]

    # " 99/99" — reserved up front so numbering can never push a post over.
    counter_room = 7
    budget = max(limit - counter_room, 40)
    first_budget = max(budget - (len(pin) + 2 if pin else 0), 40)

    parts: list[str] = []
    remaining = text
    while remaining and len(parts) < max_posts:
        # Post 1 gives up room to the pinned label.
        room = first_budget if not parts else budget
        if len(remaining) <= room:
            parts.append(remaining)
            break
        window = remaining[: room + 1]
        # Prefer a paragraph break, then a sentence end, then any whitespace.
        floor = room // 3
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < floor:
            sentence = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
            cut = sentence + 1 if sentence >= floor else -1
        if cut < floor:
            space = window.rfind(" ")
            cut = space if space > 0 else room
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if pin:
        parts[0] = f"{parts[0]}\n\n{pin}"
    if len(parts) == 1:
        return parts
    total = len(parts)
    return [f"{part} {index}/{total}" for index, part in enumerate(parts, start=1)]


class Twitter(SocialPlatform):
    name = PlatformName.twitter
    capabilities = Capabilities(
        supports_text=True,
        supports_image=True,
        supports_video=True,
        needs_public_media_url=False,
        default_orientation="landscape",
        caption_limit=280,
        notes="280 chars per post, auto-threaded beyond that. "
              "Up to 4 images OR 1 video. Media via chunked upload (media.write scope).",
        # X allows four images in one post — the same "carousel" idea, so the
        # publish tool's item-count check covers it without special-casing.
        supports_carousel=True,
        max_carousel_items=4,
        # Anything over 280 becomes a thread rather than being cut off.
        supports_threads=True,
        max_thread_posts=25,
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
            if r.status_code >= 400:
                raise self._api_error(r)
            data = r.json().get("data", {})
        return Identity(external_id=data.get("id", ""), handle=data.get("username", ""))

    async def _upload_media(self, client, access_token, path, media_kind) -> str:
        """Chunked upload via the v2 **sub-path** endpoints, returning the media id.

        X documents two shapes: the legacy ``command=INIT|APPEND|FINALIZE`` form
        parameters on ``POST /2/media/upload``, and dedicated
        ``/initialize``, ``/{id}/append``, ``/{id}/finalize`` endpoints. The
        command form rejected our INIT with a bare 400 ("One or more parameters
        to your request was invalid") — it is the migration-era shell, and
        ``initialize`` wants a **JSON** body with ``total_bytes`` as a real
        integer, not a form field. Use the dedicated endpoints; they are the
        documented contract now.
        """
        data = read_bytes(path)   # blob-aware: falls back to Azure when local is gone
        media_type = _media_type(data, path, media_kind)
        category = "tweet_video" if media_kind == "video" else "tweet_image"
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"{API}/media/upload"

        # INITIALIZE — JSON, and total_bytes must be a number.
        r = await client.post(f"{url}/initialize", headers=headers,
                              json={"media_type": media_type, "total_bytes": len(data),
                                    "media_category": category})
        if r.status_code >= 400:
            raise self._api_error(r)
        body = r.json()
        media_id = str((body.get("data") or body).get("id") or body.get("media_id") or "")
        if not media_id:
            raise RuntimeError(f"X media initialize returned no id: {str(body)[:300]}")

        # APPEND (chunked) — multipart, one segment per chunk.
        for idx, start in enumerate(range(0, len(data), _CHUNK)):
            chunk = data[start:start + _CHUNK]
            r = await client.post(f"{url}/{media_id}/append", headers=headers,
                                  data={"segment_index": str(idx)},
                                  files={"media": ("chunk", chunk, "application/octet-stream")})
            if r.status_code >= 400:
                raise self._api_error(r)

        # FINALIZE
        r = await client.post(f"{url}/{media_id}/finalize", headers=headers)
        if r.status_code >= 400:
            raise self._api_error(r)
        info = r.json().get("data", r.json())
        processing = info.get("processing_info")

        # STATUS poll (videos)
        while processing and processing.get("state") in {"pending", "in_progress"}:
            await asyncio.sleep(processing.get("check_after_secs", 3))
            r = await client.get(url, headers=headers,
                                 params={"command": "STATUS", "media_id": media_id})
            if r.status_code >= 400:
                raise self._api_error(r)
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
        caps = self.capabilities
        paths = [p for p in (asset_paths or [asset_path]) if p]
        # One video, or up to four images — X refuses a mixed set.
        if media_kind == "video":
            paths = paths[:1]
        else:
            paths = paths[: caps.max_carousel_items]

        # The disclosure label is appended to the caption, so on a thread it would
        # land on the LAST post — unseen by anyone who only meets post 1 in their
        # timeline. Pin it to the first instead.
        from .. import disclosure

        posts = split_thread(caption, caps.caption_limit, caps.max_thread_posts,
                             pin_suffix=disclosure.label() if disclosure.enabled(instruction)
                             else "")
        if not posts:
            posts = [""]

        async with httpx.AsyncClient(timeout=120) as client:
            media_ids: list[str] = []
            # No os.path.exists gate: the bytes may live in blob storage.
            if media_kind in {"image", "video"} and paths:
                media_ids = [await self._upload_media(client, access_token, path, media_kind)
                             for path in paths]

            first_id = ""
            reply_to = ""
            for index, text in enumerate(posts):
                payload: dict = {"text": text}
                community_id = str((account.meta or {}).get("community_id", "")).strip()
                if community_id:
                    payload["community_id"] = community_id
                # Media rides on the FIRST post: that is the one shown in a
                # timeline, and X would otherwise repeat the image down the thread.
                if media_ids and index == 0:
                    payload["media"] = {"media_ids": media_ids}
                if reply_to:
                    payload["reply"] = {"in_reply_to_tweet_id": reply_to}
                r = await client.post(f"{API}/tweets", headers=headers, json=payload)
                if r.status_code >= 400:
                    if first_id:
                        # The thread is already partly public; report what exists
                        # rather than losing the id of what did go out.
                        logger.error("Thread broke at post %d/%d: %s",
                                     index + 1, len(posts), self._api_error(r))
                        break
                    raise self._api_error(r)
                data = r.json().get("data", {})
                reply_to = data.get("id", "")
                if index == 0:
                    first_id, first_data = reply_to, data

        if len(posts) > 1:
            logger.info("Posted an X thread of %d posts (%d chars)", len(posts), len(caption or ""))
        handle = account.handle or "i"
        return PublishResult(
            url=f"https://x.com/{handle}/status/{first_id}" if first_id else "",
            external_id=first_id,
            raw={**first_data, "thread_posts": len(posts)} if first_id else {})


    # ------------------------------------------------------------------ #
    # Reading and engagement
    #
    # NOTE ON BILLING: since February 2026 the X API is PAY-PER-USE with no free
    # tier — you buy credits and every call, read or write, spends them. An
    # account out of credits gets 402 on everything, including posting. The tools
    # surface that verbatim rather than pretending the account is empty, because
    # "no posts", "not allowed" and "out of credits" need very different
    # responses from the agent.
    # ------------------------------------------------------------------ #
    TWEET_FIELDS = "id,text,created_at,public_metrics,conversation_id,referenced_tweets"

    @staticmethod
    def _api_error(response: httpx.Response) -> RuntimeError:
        """X errors are JSON; surface the reason instead of a bare status code.

        402 gets spelled out because httpx's own message ("Client error '402
        Payment Required'") tells you nothing actionable, and the cause is not a
        bug in the post: X moved to **pay-per-use credits** in February 2026, so
        an account with no credits cannot publish at all.

        ``detail`` and ``errors[]`` are BOTH reported. On a 400 the top-level
        detail is the useless generic "One or more parameters to your request
        was invalid" while ``errors[].message`` names the actual parameter —
        reading detail alone turned a precise complaint into a guessing game
        during the media-upload migration.
        """
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = {}
        specifics = [str(e.get("message") or e.get("detail") or "").strip()
                     for e in (body.get("errors") or []) if isinstance(e, dict)]
        specifics = [s for s in specifics if s]
        detail = body.get("detail") or body.get("title") or (response.text or "")[:300]
        for extra in specifics:
            if extra not in detail:
                detail = f"{detail} ({extra})" if detail else extra
        hint = ""
        if response.status_code == 402:
            hint = (" — this is BILLING, not a problem with the post. The X API is "
                    "pay-per-use and your developer account has no credits left. "
                    "Buy credits at https://console.x.com and retry; nothing in the "
                    "post or the account connection needs changing.")
        elif response.status_code in (401, 403):
            hint = (" — the token is rejected or the app lacks access for this call. "
                    "Check the app's permissions and that the account is still "
                    "connected; reads and writes both consume API credits.")
        elif response.status_code == 429:
            hint = " — rate limited; wait before retrying."
        # X support can trace a failed request by this id. It is safe to show:
        # unlike Authorization, it grants no access and expires with X's logs.
        headers = getattr(response, "headers", {}) or {}
        request_id = (headers.get("x-request-id") or headers.get("x-transaction-id")
                      or headers.get("x-client-transaction-id") or "")
        trace = f" [X request id: {request_id}]" if request_id else ""
        return RuntimeError(f"X API {response.status_code}: {detail}{trace}{hint}")

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

    async def list_communities(self, access_token: str, account: Account) -> list[dict]:
        """Communities this X user has joined, suitable as post targets."""
        payload = await self._get(access_token, f"users/{account.external_id}/communities", {
            "max_results": 100, "community.fields": "id,name,description,member_count"})
        return [{"id": str(c.get("id", "")), "name": c.get("name", ""),
                 "description": c.get("description", "")}
                for c in payload.get("data", []) or [] if c.get("id")]

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

        ``None`` when it cannot tell — including a 402/403, where "cannot check"
        must not be mistaken for "was deleted".
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
