"""TikTok — Content Posting API (Direct Post).

Video-only. OAuth 2.0 with ``video.publish`` (Direct Post) / ``video.upload`` +
``user.info.basic``. TikTok uses ``client_key`` (not ``client_id``) and its own
token endpoint, so the OAuth methods are overridden here.

Direct Post flow (FILE_UPLOAD):
    1. POST /v2/post/publish/video/init/  (post_info + source_info) -> {publish_id, upload_url}
    2. PUT the bytes to upload_url with a Content-Range header
    3. POST /v2/post/publish/status/fetch/  until PUBLISH_COMPLETE

Notes: unaudited apps are forced to ``privacy_level=SELF_ONLY`` until you pass
TikTok's audit. AI-generated content must be labelled — we set the AIGC flag.
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import urlencode

import httpx

from ..auth.oauth import TokenBundle
from .. import disclosure
from ..assets import read_bytes
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

OPEN_API = "https://open.tiktokapis.com/v2"


class TikTok(SocialPlatform):
    name = PlatformName.tiktok
    capabilities = Capabilities(
        supports_text=False,
        supports_image=False,
        supports_video=True,
        needs_public_media_url=False,
        default_orientation="portrait",
        caption_limit=2200,
        notes="Video only. Unaudited apps post as SELF_ONLY. AI content is AIGC-labelled.",
    )
    auth_endpoint = "https://www.tiktok.com/v2/auth/authorize/"
    token_endpoint = f"{OPEN_API}/oauth/token/"
    scopes = ["user.info.basic", "video.publish", "video.upload"]

    # TikTok uses client_key + comma-separated scopes.
    def authorize_url(self, *, redirect_uri: str, state: str, code_challenge: str | None = None) -> str:
        params = {
            "client_key": self.creds.client_id,
            "scope": ",".join(self.scopes),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{self.auth_endpoint}?{urlencode(params)}"

    async def exchange_code(self, *, code, redirect_uri, code_verifier=None) -> TokenBundle:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(self.token_endpoint, data={
                "client_key": self.creds.client_id,
                "client_secret": self.creds.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
            r.raise_for_status()
            return TokenBundle.from_response(r.json())

    async def refresh(self, refresh_token: str) -> TokenBundle:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(self.token_endpoint, data={
                "client_key": self.creds.client_id,
                "client_secret": self.creds.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
            r.raise_for_status()
            return TokenBundle.from_response(r.json())

    async def fetch_identity(self, access_token: str) -> Identity:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{OPEN_API}/user/info/",
                                 params={"fields": "open_id,union_id,display_name"},
                                 headers={"Authorization": f"Bearer {access_token}"})
            r.raise_for_status()
            user = r.json().get("data", {}).get("user", {})
        return Identity(external_id=user.get("open_id", ""), handle=user.get("display_name", ""))

    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind,
                      instruction=None) -> PublishResult:
        if media_kind != "video" or not asset_path:
            raise RuntimeError("TikTok requires a video asset; generate a video first.")
        # Read up front: the bytes may live in blob storage rather than on disk.
        video_bytes = read_bytes(asset_path)
        size = len(video_bytes)
        privacy = os.getenv("TIKTOK_PRIVACY", "SELF_ONLY")
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        init_body = {
            "post_info": {
                "title": caption[: self.capabilities.caption_limit],
                "privacy_level": privacy,
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
                # AI disclosure: the field is `is_aigc` (NOT `is_ai_generated`,
                # which the API silently ignores). True renders TikTok's
                # "Creator labeled as AI-generated" tag on the video.
                **disclosure.native_flags("tiktok", instruction),
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,          # single-chunk (fine for short clips < 64MB)
                "total_chunk_count": 1,
            },
        }
        async with httpx.AsyncClient(timeout=None) as client:
            init = await client.post(f"{OPEN_API}/post/publish/video/init/", headers=headers, json=init_body)
            init.raise_for_status()
            data = init.json().get("data", {})
            publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
            if not upload_url:
                raise RuntimeError(f"TikTok init returned no upload_url: {init.json()}")

            await client.put(upload_url, content=video_bytes, headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes 0-{size - 1}/{size}",
                })

            status, url = await self._poll_status(client, headers, publish_id)
        return PublishResult(url=url, external_id=publish_id or "", raw={"status": status})

    async def _poll_status(self, client, headers, publish_id, tries=20, delay=5.0):
        for _ in range(tries):
            r = await client.post(f"{OPEN_API}/post/publish/status/fetch/",
                                  headers=headers, json={"publish_id": publish_id})
            r.raise_for_status()
            data = r.json().get("data", {})
            status = data.get("status", "")
            if status == "PUBLISH_COMPLETE":
                pid = (data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or [])
                url = f"https://www.tiktok.com/@me/video/{pid[0]}" if pid else ""
                return status, url
            if status in {"FAILED", "PUBLISH_FAILED"}:
                raise RuntimeError(f"TikTok publish failed: {data}")
            await asyncio.sleep(delay)
        return "PENDING", ""


register(PlatformName.tiktok, TikTok)
