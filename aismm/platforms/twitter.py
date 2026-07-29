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
import os

import httpx

from ..assets import read_bytes
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

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
        notes="280 chars. Media via chunked upload (media.write scope).",
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
                      instruction=None) -> PublishResult:
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        payload: dict = {"text": caption[: self.capabilities.caption_limit]}
        async with httpx.AsyncClient(timeout=120) as client:
            # No os.path.exists gate: the bytes may live in blob storage.
            if media_kind in {"image", "video"} and asset_path:
                media_id = await self._upload_media(client, access_token, asset_path, media_kind)
                payload["media"] = {"media_ids": [media_id]}
            r = await client.post(f"{API}/tweets", headers=headers, json=payload)
            r.raise_for_status()
            data = r.json().get("data", {})
        tweet_id = data.get("id", "")
        handle = account.handle or "i"
        return PublishResult(url=f"https://x.com/{handle}/status/{tweet_id}" if tweet_id else "",
                             external_id=tweet_id, raw=data)


register(PlatformName.twitter, Twitter)
