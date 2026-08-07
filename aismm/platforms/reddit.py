"""Reddit — submit posts to a subreddit (self/text + image).

Reddit is shaped unlike the feed platforms: a post goes **to a subreddit** and
**must have a title**. So this integration borrows two patterns already in the
codebase — the caption split from YouTube (first line = title, the rest = body)
and a per-account destination stored in ``account.meta`` like X's community — and
posts to ``account.meta["subreddit"]``, falling back to the member's own profile
(``u_<username>``) when none is set.

Three Reddit-specific rules this module follows:

* **A descriptive ``User-Agent`` is mandatory** — Reddit throttles or blocks the
  generic ones httpx/requests send by default, *including on the token endpoint*,
  so :meth:`exchange_code` / :meth:`refresh` are overridden to send it too.
* **A refresh token only comes back with ``duration=permanent``** on the authorize
  URL — without it the grant expires in an hour and cannot be renewed.
* **An image submit is asynchronous.** The bytes go to Reddit's S3 by an upload
  *lease*, then ``/api/submit`` accepts the object URL and returns a websocket
  (not the permalink). The post is created regardless; the returned URL is
  best-effort, so republish/duplicate-guarding leans on the media fingerprint,
  not the id.

OAuth 2.0: authorize ``/api/v1/authorize``, token ``/api/v1/access_token`` with
HTTP Basic client auth. Video is not built yet (it needs a poster frame and the
websocket handshake); ``Capabilities`` declares it unsupported so `perform_publish`
refuses it cleanly.
"""
from __future__ import annotations

import logging
import mimetypes

import httpx

from ..assets import read_bytes
from ..auth.oauth import TokenBundle
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

logger = logging.getLogger("aismm.platforms.reddit")

WWW = "https://www.reddit.com"
OAUTH = "https://oauth.reddit.com"
# Reddit asks for `platform:app-id:version (by /u/username)`. It only needs to be
# descriptive and unique — a generic UA is what gets rate-limited or blocked.
USER_AGENT = "web:aismm-ai-social-media-manager:1.0 (by AISMM)"


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT}


def _api_error(exc: httpx.HTTPStatusError) -> str:
    """Reddit's error body, with the common causes named."""
    resp = exc.response
    if resp is None:
        return str(exc)
    code = resp.status_code
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - body may not be JSON
        payload = {}
    message = ""
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or ""
    hint = {
        401: " (token expired or invalid — reconnect the account)",
        403: " (forbidden — the account may be banned from this subreddit, or the "
             "subreddit is restricted/private)",
        429: " (rate limited — Reddit is throttling; slow the schedule)",
    }.get(code, "")
    body = message or (resp.text or "")[:300]
    return f"Reddit {code}: {body}{hint}"


def _raise(exc: httpx.HTTPStatusError) -> None:
    raise RuntimeError(_api_error(exc)) from None


class Reddit(SocialPlatform):
    name = PlatformName.reddit
    capabilities = Capabilities(
        supports_text=True,
        supports_image=True,
        supports_video=False,   # needs a poster frame + websocket handshake; not built
        needs_public_media_url=False,   # bytes are uploaded to Reddit's S3 directly
        default_orientation="landscape",
        caption_limit=40000,            # selftext limit
        notes="Posts to a subreddit (set one per account; defaults to your u_ profile). "
              "First line of the caption is the title (<=300 chars); the rest is the body. "
              "Single image or self/text posts.",
    )
    auth_endpoint = f"{WWW}/api/v1/authorize"
    token_endpoint = f"{WWW}/api/v1/access_token"
    scopes = ["identity", "submit", "read"]
    token_auth_style = "basic"          # client id/secret as HTTP Basic
    scope_sep = " "
    # Without duration=permanent Reddit issues NO refresh token and the access
    # token dies in an hour with no way to renew it.
    extra_authorize_params = {"duration": "permanent"}

    # ------------------------------------------------------------------ #
    # OAuth — overridden only to send the mandatory User-Agent
    # ------------------------------------------------------------------ #
    async def exchange_code(self, *, code, redirect_uri, code_verifier=None) -> TokenBundle:
        return await self._token_request({
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})

    async def refresh(self, refresh_token: str) -> TokenBundle:
        return await self._token_request({
            "grant_type": "refresh_token", "refresh_token": refresh_token})

    async def _token_request(self, data: dict) -> TokenBundle:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                self.token_endpoint, data=data,
                auth=(self.creds.client_id, self.creds.client_secret),
                headers={"User-Agent": USER_AGENT,
                         "Content-Type": "application/x-www-form-urlencoded"})
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise(exc)
            return TokenBundle.from_response(r.json())

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #
    async def fetch_identity(self, access_token: str) -> Identity:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{OAUTH}/api/v1/me", headers=_headers(access_token))
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise(exc)
            data = r.json()
        name = data.get("name", "")
        if not name:
            raise RuntimeError("Reddit /api/v1/me returned no username.")
        return Identity(external_id=data.get("id", "") or name, handle=name)

    def _subreddit(self, account: Account) -> str:
        """The destination subreddit name (no ``r/`` prefix), or the u_ profile."""
        sr = ((account.meta or {}).get("subreddit") or "").strip()
        sr = sr.removeprefix("/r/").removeprefix("r/").strip("/")
        if sr:
            return sr
        if account.handle:
            return f"u_{account.handle}"      # posting to your own profile
        raise RuntimeError(
            "No subreddit set for this Reddit account, and no username to fall back on. "
            "Set a destination on the Accounts page.")

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #
    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind,
                      instruction=None, asset_paths=None, placement="feed") -> PublishResult:
        subreddit = self._subreddit(account)
        title, _, body = (caption or "").partition("\n")
        title = title.strip()[:300] or "Untitled"
        paths = [p for p in (asset_paths or ([asset_path] if asset_path else [])) if p]

        async with httpx.AsyncClient(timeout=120) as client:
            if media_kind == "image":
                if not paths:
                    raise RuntimeError("Reddit image post has no media asset.")
                if len(paths) > 1:
                    raise RuntimeError(
                        f"Reddit takes one image per post here; {len(paths)} were passed. "
                        f"(Galleries are a separate endpoint, not built yet.)")
                asset_url = await self._upload_asset(client, access_token, paths[0])
                data = await self._submit(client, access_token, {
                    "kind": "image", "sr": subreddit, "title": title, "url": asset_url})
            elif media_kind == "video":
                # Guarded by Capabilities too; message names the boundary anyway.
                raise RuntimeError("Reddit video posting is not supported yet.")
            else:
                data = await self._submit(client, access_token, {
                    "kind": "self", "sr": subreddit, "title": title, "text": body.strip()})
        return _result(data)

    async def _submit(self, client, token, fields: dict) -> dict:
        r = await client.post(f"{OAUTH}/api/submit", data={**fields, "api_type": "json"},
                             headers=_headers(token))
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise(exc)
        payload = r.json().get("json", {}) if isinstance(r.json(), dict) else {}
        errors = payload.get("errors") or []
        if errors:
            # Each error is [CODE, human message, field].
            detail = "; ".join(e[1] if len(e) > 1 else str(e) for e in errors)
            raise RuntimeError(f"Reddit rejected the post: {detail}")
        return payload.get("data", {}) or {}

    async def _upload_asset(self, client, token, path) -> str:
        """Lease an S3 slot, upload the bytes, return the object URL for submit."""
        data = read_bytes(path)
        name = path.split("/")[-1]
        mimetype = mimetypes.guess_type(name)[0] or "image/jpeg"
        lease = await client.post(f"{OAUTH}/api/media/asset.json",
                                 data={"filepath": name, "mimetype": mimetype},
                                 headers=_headers(token))
        try:
            lease.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise(exc)
        info = lease.json()
        args = info.get("args", {})
        action = args.get("action", "")
        if action.startswith("//"):
            action = "https:" + action
        upload_fields = {f["name"]: f["value"] for f in args.get("fields", [])}
        if not action or "key" not in upload_fields:
            raise RuntimeError("Reddit did not return a usable media upload lease.")
        # The S3 POST is a plain form upload — no Reddit auth/User-Agent header.
        up = await client.post(action, data=upload_fields,
                              files={"file": (name, data, mimetype)})
        try:
            up.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise(exc)
        return f"{action}/{upload_fields['key']}"


def _result(data: dict) -> PublishResult:
    """Build a PublishResult, deriving a permalink when the submit gave no url.

    A self post returns ``url`` directly; an image submit returns a websocket
    instead, so fall back to the fullname (``t3_<id>`` → ``redd.it/<id>``).
    """
    url = data.get("url", "")
    name = data.get("name", "") or ""
    ext = name[3:] if name.startswith("t3_") else (data.get("id", "") or "")
    if not url and ext:
        url = f"https://redd.it/{ext}"
    return PublishResult(url=url, external_id=ext, raw=data)


register(PlatformName.reddit, Reddit)
