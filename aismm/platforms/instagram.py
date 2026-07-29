"""Instagram — Meta Instagram Graph API (content publishing).

Requires an Instagram **Business/Creator** account linked to a Facebook Page, and
a Meta app with ``instagram_content_publish``. Publishing is a two-step Graph call
and, crucially, Instagram FETCHES the media from a PUBLIC URL — so the generated
asset must be reachable (the dashboard serves it at ``/assets/<file>``; set
``DASHBOARD_BASE_URL`` to a public https URL, e.g. an ngrok tunnel, when testing).

Flow:
    1. POST /{ig-user-id}/media   (image_url | video_url + media_type=REELS)  -> creation_id
    2. GET  /{creation_id}?fields=status_code  until FINISHED
    3. POST /{ig-user-id}/media_publish  (creation_id)          -> media_id
    4. GET  /{media_id}?fields=permalink                        -> permalink

Two rules this module follows, both learned the hard way:

* **The access token never goes in the URL.** Graph accepts it as
  ``Authorization: Bearer``; as a query parameter it ends up in httpx's exception
  messages and from there in the service log, leaking a live Page token.
* **Always surface Graph's error body.** ``raise_for_status`` alone reports only
  "400 Bad Request"; the JSON body carries the message, ``code``/``error_subcode``
  and ``fbtrace_id`` that actually identify the problem (same reasoning as
  ``sora_client.format_http_error``).
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..assets import public_url
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

GRAPH_VERSION = "v21.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

logger = logging.getLogger("aismm.platforms.instagram")

# A container can report FINISHED a moment before the media id is publishable;
# Graph answers the early publish with code 9007 / "Media ID is not available".
_NOT_READY_CODES = {9007}
_PUBLISH_RETRIES = 5
_PUBLISH_RETRY_DELAY = 6.0

# Meta's own processing failed on media it already fetched. Undocumented and
# widely reported as intermittent, so the whole container is rebuilt once.
_CONTAINER_RETRIES = 2
_CONTAINER_RETRY_DELAY = 20.0


class ContainerError(RuntimeError):
    """Instagram accepted the request but its media processing failed."""


def _auth(access_token: str) -> dict:
    """Graph accepts the token as a bearer header — keep it out of the URL."""
    return {"Authorization": f"Bearer {access_token}"}


def _safe_url(response: httpx.Response | None) -> str:
    """Request URL without its query string (which may carry a token)."""
    if response is None or response.request is None:
        return ""
    url = response.request.url
    return f"{url.scheme}://{url.host}{url.path}"


def _graph_error(exc: httpx.HTTPStatusError) -> tuple[str, dict]:
    """Turn a Graph error response into an actionable message + its error dict."""
    resp = exc.response
    if resp is None:
        return str(exc), {}
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - body may not be JSON
        payload = {}
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not err:
        body = (resp.text or "")[:500]
        return f"Instagram Graph {resp.status_code} at {_safe_url(resp)}: {body}", {}

    detail = " · ".join(
        f"{key}={err[key]}" for key in
        ("code", "error_subcode", "type", "error_user_title", "error_user_msg", "fbtrace_id")
        if err.get(key) not in (None, "")
    )
    message = err.get("message") or "no message"
    return (f"Instagram Graph {resp.status_code}: {message}"
            + (f" [{detail}]" if detail else "")), err


def _raise_graph(exc: httpx.HTTPStatusError) -> None:
    """Re-raise a Graph failure with the body included and no token in the text."""
    message, _err = _graph_error(exc)
    raise RuntimeError(message) from None


class Instagram(SocialPlatform):
    name = PlatformName.instagram
    capabilities = Capabilities(
        supports_text=False,           # IG posts always carry media
        supports_image=True,
        supports_video=True,           # Reels
        needs_public_media_url=True,
        default_orientation="portrait",
        caption_limit=2200,
        notes="Business/Creator account required. Reels: 9:16, 5–90s. Media must be at a public URL. "
              "Images: JPEG only, 8MB, aspect 4:5–1.91:1.",
        # Meta's published limits for image containers. Anything else comes back
        # as "Media download has failed" with no hint that the FILE is the problem.
        image_formats=("jpg", "jpeg"),
        max_image_bytes=8 * 1024 * 1024,
        min_image_ratio=0.8,       # 4:5
        max_image_ratio=1.91,      # 1.91:1
        max_image_width=1440,
    )
    auth_endpoint = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
    token_endpoint = f"{GRAPH}/oauth/access_token"
    scopes = [
        "instagram_basic",
        "instagram_content_publish",
        "pages_show_list",
        "pages_read_engagement",
        "business_management",
    ]
    scope_sep = ","

    async def fetch_identity(self, access_token: str) -> Identity:
        """Resolve the IG business account + its Page access token from a user token."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{GRAPH}/me/accounts",
                params={"fields": "name,access_token,instagram_business_account{id,username}"},
                headers=_auth(access_token),
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise_graph(exc)
            pages = resp.json().get("data", [])
        for page in pages:
            iba = page.get("instagram_business_account")
            if iba:
                return Identity(
                    external_id=iba["id"],
                    handle=iba.get("username") or page.get("name", ""),
                    # Publishing uses the PAGE access token; store it as the account token.
                    meta={"access_token": page.get("access_token", access_token),
                          "page_name": page.get("name", "")},
                )
        raise RuntimeError(
            "No Instagram Business account found on any Facebook Page for this login. "
            "Link an IG Business/Creator account to a Page and grant instagram_content_publish."
        )

    async def _wait_finished(self, client, creation_id, token, tries=30, delay=6.0):
        """Poll a container until it is FINISHED.

        Done for images as well as Reels: an image container is usually ready
        immediately, but publishing one that isn't yet is a 400 that reads like a
        permissions problem. ``status`` carries Graph's explanation when the
        container errors.
        """
        status = None
        for attempt in range(tries):
            r = await client.get(f"{GRAPH}/{creation_id}",
                                 params={"fields": "status_code,status"},
                                 headers=_auth(token))
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise_graph(exc)
            body = r.json()
            previous, status = status, body.get("status_code")
            if status != previous:
                logger.info("Instagram container %s: %s (%ds elapsed)",
                            creation_id, status, int(attempt * delay))
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                detail = body.get("status") or "no detail"
                logger.error("Instagram container %s failed: %s", creation_id, detail)
                raise ContainerError(f"Instagram media container {status}: {detail}")
            await asyncio.sleep(delay)
        raise TimeoutError(
            f"Instagram container not FINISHED after {int(tries * delay)}s "
            f"(last status_code={status!r})")

    async def _publish_container(self, client, ig_user_id, creation_id, token):
        """POST media_publish, retrying while Graph says the media isn't ready yet."""
        for attempt in range(_PUBLISH_RETRIES):
            r = await client.post(f"{GRAPH}/{ig_user_id}/media_publish",
                                  data={"creation_id": creation_id},
                                  headers=_auth(token))
            if r.status_code < 400:
                return r.json()["id"]
            exc = httpx.HTTPStatusError("publish failed", request=r.request, response=r)
            message, err = _graph_error(exc)
            if err.get("code") in _NOT_READY_CODES and attempt < _PUBLISH_RETRIES - 1:
                logger.info("Instagram media not publishable yet (attempt %d/%d): %s",
                            attempt + 1, _PUBLISH_RETRIES, message)
                await asyncio.sleep(_PUBLISH_RETRY_DELAY)
                continue
            raise RuntimeError(message)
        raise RuntimeError("Instagram media never became publishable")

    async def _log_media_preflight(self, client, media_url, asset_path, media_kind) -> None:
        """Describe the media Instagram is about to fetch. Never fatal.

        Answers the two questions a container failure can't: *is the URL actually
        serving the file* (a private blob container returns 403 here), and *what
        is the file* (format, dimensions, aspect ratio — the things Meta rejects
        without saying so).
        """
        try:
            from pathlib import Path

            local = Path(asset_path)
            size = local.stat().st_size if local.exists() else None
            details = f"{local.name} kind={media_kind}"
            if size is not None:
                details += f" bytes={size:,}"
            if media_kind == "image" and local.exists():
                try:
                    from PIL import Image

                    with Image.open(local) as image:
                        details += (f" format={image.format} mode={image.mode} "
                                    f"size={image.width}x{image.height} "
                                    f"ratio={image.width / image.height:.3f}")
                except Exception as exc:  # noqa: BLE001
                    details += f" (unreadable image: {exc})"
            logger.info("Instagram media: %s", details)

            head = await client.head(media_url, follow_redirects=True, timeout=30)
            logger.info("Instagram media URL check: HTTP %s content-type=%s length=%s url=%s",
                        head.status_code, head.headers.get("content-type", "?"),
                        head.headers.get("content-length", "?"), media_url)
            if head.status_code >= 400:
                logger.error("Instagram cannot fetch the media URL (HTTP %s). If this is Azure "
                             "Blob, set the container access level to 'Blob (anonymous read)'.",
                             head.status_code)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never block a post
            logger.warning("Media preflight check failed (continuing): %s", exc)

    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind,
                      instruction=None) -> PublishResult:
        ig_user_id = account.external_id
        media_url = public_url(asset_path)
        if not media_url:
            raise RuntimeError("Instagram needs a media asset; generate an image or reel first.")
        if media_url.startswith(("http://127.", "http://localhost")):
            raise RuntimeError(
                "Instagram must fetch media from a PUBLIC url. Set DASHBOARD_BASE_URL to a "
                "public https address (e.g. an ngrok tunnel) so /assets/<file> is reachable."
            )
        async with httpx.AsyncClient(timeout=120) as client:
            # Log exactly what Instagram is about to fetch. A container that
            # errors during PROCESSING gives no clue about the file, so record
            # the file's own properties before handing over the URL.
            await self._log_media_preflight(client, media_url, asset_path, media_kind)

            create_data = {"caption": caption}
            if media_kind == "video":
                create_data.update({"media_type": "REELS", "video_url": media_url})
            else:
                create_data["image_url"] = media_url

            # Meta's processing failures (e.g. the undocumented 2207076) are
            # widely reported as intermittent, so rebuild the container once.
            for attempt in range(_CONTAINER_RETRIES):
                r = await client.post(f"{GRAPH}/{ig_user_id}/media", data=create_data,
                                      headers=_auth(access_token))
                try:
                    r.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    _raise_graph(exc)
                creation_id = r.json()["id"]
                logger.info("Instagram container %s created (%s, caption %d chars)",
                            creation_id, media_kind, len(caption))

                # Reels take minutes to transcode; images are usually instant but
                # a container that isn't FINISHED cannot be published either way.
                tries = 30 if media_kind == "video" else 10
                try:
                    await self._wait_finished(client, creation_id, access_token, tries=tries)
                    break
                except ContainerError:
                    if attempt == _CONTAINER_RETRIES - 1:
                        raise
                    logger.warning("Rebuilding the Instagram container after a processing "
                                   "failure (attempt %d/%d)", attempt + 1, _CONTAINER_RETRIES)
                    await asyncio.sleep(_CONTAINER_RETRY_DELAY)

            media_id = await self._publish_container(client, ig_user_id, creation_id, access_token)

            perma = await client.get(f"{GRAPH}/{media_id}", params={"fields": "permalink"},
                                     headers=_auth(access_token))
            url = perma.json().get("permalink", "") if perma.status_code == 200 else ""
        logger.info("Instagram published media %s", media_id)
        return PublishResult(url=url, external_id=media_id, raw={"media_id": media_id})


register(PlatformName.instagram, Instagram)
