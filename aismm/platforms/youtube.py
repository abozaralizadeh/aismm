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

import os

import httpx

from .. import disclosure
from ..assets import read_bytes
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


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
    )
    auth_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    scopes = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
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

    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind) -> PublishResult:
        if media_kind != "video" or not asset_path:
            raise RuntimeError("YouTube requires a video asset; generate a video first.")
        # Read up front: the bytes may live in blob storage rather than on disk.
        video_bytes = read_bytes(asset_path)
        title, _, description = caption.partition("\n")
        metadata = {
            "snippet": {"title": title[:100] or "Untitled", "description": description.strip()},
            "status": {"privacyStatus": os.getenv("YOUTUBE_PRIVACY", "private"),
                       "selfDeclaredMadeForKids": False,
                       # containsSyntheticMedia drives YouTube's altered/synthetic
                       # content disclosure ("How this content was made").
                       **disclosure.native_flags("youtube")},
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
        return PublishResult(url=f"https://youtu.be/{video_id}" if video_id else "",
                             external_id=video_id, raw=data)


register(PlatformName.youtube, YouTube)
