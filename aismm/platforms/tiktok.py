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
            user = self._check(r).get("data", {}).get("user", {})
        return Identity(external_id=user.get("open_id", ""), handle=user.get("display_name", ""))

    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind,
                      instruction=None, asset_paths=None, placement="feed") -> PublishResult:
        """Upload one video.

        ``asset_paths`` / ``placement`` are part of the :class:`SocialPlatform`
        contract and are always passed; TikTok is single-video here, so extra
        paths are refused rather than silently dropped.
        """
        if asset_paths and len(asset_paths) > 1:
            raise RuntimeError(
                f"TikTok takes one video per post; {len(asset_paths)} assets were passed. "
                f"Publish them separately.")
        asset_path = asset_path or (asset_paths[0] if asset_paths else "")
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
            data = self._check(init).get("data", {})
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
            data = self._check(r).get("data", {})
            status = data.get("status", "")
            if status == "PUBLISH_COMPLETE":
                pid = (data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or [])
                url = f"https://www.tiktok.com/@me/video/{pid[0]}" if pid else ""
                return status, url
            if status in {"FAILED", "PUBLISH_FAILED"}:
                raise RuntimeError(f"TikTok publish failed: {data}")
            await asyncio.sleep(delay)
        return "PENDING", ""

    def _check(self, response: httpx.Response) -> dict:
        """Raise a spelled-out error on an HTTP failure OR a TikTok error code.

        TikTok signals failure two ways: a non-2xx status, and a 2xx whose body
        carries ``error.code != "ok"``. ``raise_for_status`` catches only the
        first and, worse, throws away the JSON body that names the real cause —
        the same lesson X taught (``_upload_media``/``fetch_identity`` leaked the
        raw message). Route every Open-API call through here.
        """
        try:
            body = response.json() if response.content else {}
        except Exception:  # noqa: BLE001
            body = {}
        code = str((body.get("error") or {}).get("code") or "").strip()
        if response.status_code >= 400 or (code and code != "ok"):
            raise self._api_error(response)
        return body

    @staticmethod
    def _api_error(response: httpx.Response) -> RuntimeError:
        """Surface TikTok's reason, and say when a 403 is NOT about the video.

        A 403 on ``/post/publish/...`` is almost always a PERMISSION problem, not
        the clip: the token is missing the ``video.publish`` scope Direct Post
        needs, or the app has not passed TikTok's audit / URL-ownership
        verification. Regenerating the video fixes none of them, so the reflex to
        re-run the agent just spends money — name the cause instead.
        """
        try:
            err = (response.json() or {}).get("error", {}) or {}
        except Exception:  # noqa: BLE001
            err = {}
        code = str(err.get("code") or "").strip()
        message = str(err.get("message") or "").strip()
        log_id = str(err.get("log_id") or "").strip()
        detail = message or (response.text or "")[:300]
        low = f"{code} {message}".lower()
        hint = ""
        if response.status_code in (401, 403) or (code and code != "ok"):
            if "scope" in low:
                hint = (" — the connected account's token is MISSING the video.publish "
                        "scope that Direct Post needs. Reconnect the TikTok account on the "
                        "Accounts page and grant video.publish; regenerating the video will "
                        "not help.")
            elif any(w in low for w in ("audit", "unaudited", "url", "domain", "verif")):
                hint = (" — this is an APP-APPROVAL problem, not the video: TikTok has not "
                        "audited the app or verified its URL ownership. Finish the app's "
                        "review and domain verification in TikTok for Developers; unaudited "
                        "apps can only post as SELF_ONLY.")
            elif response.status_code in (401, 403):
                hint = (" — the token is rejected or the app lacks permission for this call. "
                        "Reconnect the account and confirm the app is approved for the "
                        "Content Posting API; this is not a problem with the video. A 403 on "
                        "publish/init most often means the app is still unaudited or the "
                        "video.publish scope was not granted.")
        elif response.status_code == 429:
            hint = " — rate limited; wait before retrying."
        elif response.status_code >= 500:
            hint = (" — this is TikTok's own service failing, not your video or token. "
                    "Retry later with 'Publish this again' rather than regenerating.")
        # TikTok support can trace a request by log_id; it grants no access.
        trace = f" [TikTok log_id: {log_id}]" if log_id else ""
        label = f"{code}: " if code and code != "ok" else ""
        return RuntimeError(f"TikTok API {response.status_code}: {label}{detail}{trace}{hint}")


register(PlatformName.tiktok, TikTok)
