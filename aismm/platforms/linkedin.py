"""LinkedIn — Posts API (member share).

Posts to the signed-in **member's** feed (`urn:li:person:{sub}`). OAuth 2.0
authorization code, with the *Sign In with LinkedIn using OpenID Connect* product
for identity (``/v2/userinfo``) and *Share on LinkedIn* for ``w_member_social``.

Publishing uses the versioned REST surface, which needs two headers on every
call: ``LinkedIn-Version`` (a ``YYYYMM`` string) and
``X-Restli-Protocol-Version: 2.0.0``.

Flow:
    * text   →  POST /rest/posts  {author, commentary, ...}
    * image  →  POST /rest/images?action=initializeUpload  -> uploadUrl + image urn
                PUT bytes to uploadUrl
                POST /rest/posts with content.media = {id: <image urn>}
    * video  →  POST /rest/videos?action=initializeUpload -> upload instructions
                PUT each byte range (collect part ids)
                POST /rest/videos?action=finalizeUpload
                POST /rest/posts with content.media = {id: <video urn>}

Organisation (Company Page) posting would swap the author URN for
``urn:li:organization:{id}`` and need ``w_organization_social`` + an admin role —
left for later; this posts as the member.
"""
from __future__ import annotations

import logging
import mimetypes

import httpx

from ..assets import read_bytes
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

logger = logging.getLogger("aismm.platforms.linkedin")

API = "https://api.linkedin.com"
REST = f"{API}/rest"
# LinkedIn versions its REST API by month; a stale value 426s ("Unsupported
# version"). Bump when LinkedIn deprecates one — the two callers both read this.
LINKEDIN_VERSION = "202405"


def _rest_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _api_error(exc: httpx.HTTPStatusError) -> str:
    """LinkedIn's JSON error body, without leaking the token in the URL."""
    resp = exc.response
    if resp is None:
        return str(exc)
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - body may not be JSON
        payload = {}
    message = ""
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error_description") or ""
    body = message or (resp.text or "")[:400]
    return f"LinkedIn {resp.status_code}: {body}"


def _raise(exc: httpx.HTTPStatusError) -> None:
    raise RuntimeError(_api_error(exc)) from None


class LinkedIn(SocialPlatform):
    name = PlatformName.linkedin
    capabilities = Capabilities(
        supports_text=True,
        supports_image=True,
        supports_video=True,
        needs_public_media_url=False,   # bytes are uploaded directly
        default_orientation="landscape",
        caption_limit=3000,             # commentary limit on a member share
        notes="Posts to the member's LinkedIn feed. One image or one video per post.",
    )
    auth_endpoint = "https://www.linkedin.com/oauth/v2/authorization"
    token_endpoint = "https://www.linkedin.com/oauth/v2/accessToken"
    # OpenID Connect for identity + w_member_social to post as the member.
    scopes = ["openid", "profile", "email", "w_member_social"]
    token_auth_style = "body"
    scope_sep = " "

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #
    async def fetch_identity(self, access_token: str) -> Identity:
        """The signed-in member, via the OpenID ``/v2/userinfo`` endpoint."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{API}/v2/userinfo",
                                 headers={"Authorization": f"Bearer {access_token}"})
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise(exc)
            data = r.json()
        sub = data.get("sub", "")
        if not sub:
            raise RuntimeError("LinkedIn userinfo returned no member id (sub). "
                               "Ensure the 'openid' and 'profile' scopes were granted.")
        handle = data.get("name") or data.get("given_name") or sub
        # Store the person URN so publishing never re-derives it.
        return Identity(external_id=sub, handle=handle,
                        meta={"author_urn": f"urn:li:person:{sub}"})

    def _author_urn(self, account: Account) -> str:
        return (account.meta or {}).get("author_urn") or f"urn:li:person:{account.external_id}"

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #
    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind,
                      instruction=None, asset_paths=None, placement="feed") -> PublishResult:
        author = self._author_urn(account)
        paths = [p for p in (asset_paths or ([asset_path] if asset_path else [])) if p]
        if paths and len(paths) > 1:
            raise RuntimeError(
                f"LinkedIn takes one media asset per post; {len(paths)} were passed. "
                f"Publish them as separate posts.")
        async with httpx.AsyncClient(timeout=120) as client:
            media = None
            if media_kind == "image" and paths:
                media = await self._upload_image(client, access_token, author, paths[0])
            elif media_kind == "video" and paths:
                media = await self._upload_video(client, access_token, author, paths[0])
            return await self._create_post(client, access_token, author, caption or "", media)

    async def _create_post(self, client, token, author, commentary, media) -> PublishResult:
        body = {
            "author": author,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if media:
            body["content"] = {"media": media}
        r = await client.post(f"{REST}/posts", json=body,
                              headers={**_rest_headers(token), "Content-Type": "application/json"})
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise(exc)
        # The Posts API returns the new post's URN in a header, not the body.
        urn = r.headers.get("x-restli-id") or r.headers.get("x-linkedin-id", "")
        url = f"https://www.linkedin.com/feed/update/{urn}" if urn else ""
        return PublishResult(url=url, external_id=urn, raw={"urn": urn})

    async def _upload_image(self, client, token, author, path) -> dict:
        """Register an image upload, PUT the bytes, return the media reference."""
        init = await client.post(
            f"{REST}/images?action=initializeUpload", json={
                "initializeUploadRequest": {"owner": author}},
            headers={**_rest_headers(token), "Content-Type": "application/json"})
        try:
            init.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise(exc)
        value = init.json().get("value", {})
        upload_url, image_urn = value.get("uploadUrl"), value.get("image")
        if not upload_url or not image_urn:
            raise RuntimeError("LinkedIn did not return an image upload URL.")
        data = read_bytes(path)
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        put = await client.put(upload_url, content=data,
                              headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": content_type})
        try:
            put.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise(exc)
        return {"id": image_urn}

    async def _upload_video(self, client, token, author, path) -> dict:
        """Register a video upload, PUT each byte range, finalize, return the ref.

        LinkedIn splits a video into byte ranges (``uploadInstructions``); each PUT
        answers with an ``ETag`` that the finalize step needs back as a part id.
        """
        data = read_bytes(path)
        init = await client.post(
            f"{REST}/videos?action=initializeUpload", json={
                "initializeUploadRequest": {
                    "owner": author, "fileSizeBytes": len(data),
                    "uploadCaptions": False, "uploadThumbnail": False}},
            headers={**_rest_headers(token), "Content-Type": "application/json"})
        try:
            init.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise(exc)
        value = init.json().get("value", {})
        video_urn = value.get("video")
        instructions = value.get("uploadInstructions", [])
        upload_token = value.get("uploadToken", "")
        if not video_urn or not instructions:
            raise RuntimeError("LinkedIn did not return video upload instructions.")
        part_ids = []
        for step in instructions:
            first, last = step.get("firstByte", 0), step.get("lastByte", len(data) - 1)
            chunk = data[first:last + 1]
            put = await client.put(step["uploadUrl"], content=chunk,
                                  headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "application/octet-stream"})
            try:
                put.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise(exc)
            etag = put.headers.get("ETag") or put.headers.get("etag", "")
            part_ids.append(etag.strip('"'))
        fin = await client.post(
            f"{REST}/videos?action=finalizeUpload", json={
                "finalizeUploadRequest": {
                    "video": video_urn, "uploadToken": upload_token,
                    "uploadedPartIds": part_ids}},
            headers={**_rest_headers(token), "Content-Type": "application/json"})
        try:
            fin.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise(exc)
        return {"id": video_urn}


register(PlatformName.linkedin, LinkedIn)
