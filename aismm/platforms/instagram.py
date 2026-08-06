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
import re
from datetime import datetime, timedelta, timezone

import httpx

from .. import disclosure
from ..assets import kind_from_path, public_url
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

GRAPH_VERSION = "v21.0"
# One request is capped at 100 by Graph; this bounds how many pages we walk.
MAX_MEDIA_PAGE_TOTAL = 500

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


class RateLimited(RuntimeError):
    """Meta is refusing this action for volume reasons, not content reasons.

    Code 4 is the app-level request limit; 17 the per-user limit; 32 the page
    limit; subcode 2207051 is the same thing wearing integrity language ("action
    is blocked — we restrict certain activity to protect our community").

    This must NEVER be retried in the same run and, more importantly, must stop
    the *next* scheduled run from trying again: repeated attempts against a
    blocked account extend the block. Callers put the account in a cooldown.
    """

    def __init__(self, message: str, retry_after_seconds: int = 3600):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


# Volume/integrity refusals, as opposed to "this media is wrong".
_RATE_LIMIT_CODES = {4, 17, 32}
_BLOCKED_SUBCODES = {2207051}

# "Unsupported get request. Object does not exist…" — what Graph says about a
# media id that has been deleted. Used to tell "gone" apart from "can't reach it".
_GONE_CODES = {803}
_GONE_SUBCODES = {33}

# How much of the profile grid to scan when deciding whether a post was archived.
# Graph has no is_archived field; archived posts are simply missing from this
# listing, so the scan depth is also how far back "archived" can be detected.
_GRID_SCAN_LIMIT = 100

# How far back to look when checking whether a failed media_publish actually
# published. Generous enough to cover a slow container, short enough that an
# earlier post of the same caption can't be mistaken for this one.
_RECONCILE_WINDOW_SECONDS = 900


def _caption_key(caption: str | None) -> str:
    """Normalized caption for comparing ours against what Graph reports back.

    Graph can return the caption with different whitespace than we submitted, so
    compare on collapsed whitespace over a bounded prefix.
    """
    return re.sub(r"\s+", " ", (caption or "").strip())[:200].lower()


def _parse_graph_time(value) -> datetime | None:
    """Graph timestamps look like ``2026-07-30T12:33:41+0000``."""
    if not value:
        return None
    text = str(value)
    # fromisoformat wants +00:00, Graph sends +0000.
    if re.search(r"[+-]\d{4}$", text):
        text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
    """Re-raise a Graph failure with the body included and no token in the text.

    Volume refusals become :class:`RateLimited` so callers can back off instead of
    treating them like a content problem and trying again.
    """
    message, err = _graph_error(exc)
    code, subcode = err.get("code"), err.get("error_subcode")
    if code in _RATE_LIMIT_CODES or subcode in _BLOCKED_SUBCODES:
        raise RateLimited(message) from None
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
        min_image_ratio=0.8,       # 4:5   — FEED posts only
        max_image_ratio=1.91,      # 1.91:1
        max_image_width=1440,
        # A story is 9:16 (0.5625). Padding one up to the feed's 4:5 minimum
        # publishes it pillarboxed — bars down both sides — which is what made
        # stories "not work" while still technically succeeding.
        story_min_image_ratio=0.5,
        story_max_image_ratio=1.91,
        supports_carousel=True,
        supports_stories=True,
        max_carousel_items=10,
        supports_comments=True,
        supports_insights=True,
    )
    auth_endpoint = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
    token_endpoint = f"{GRAPH}/oauth/access_token"
    # Scopes are split by what they cost you if the app cannot request them.
    #
    # Meta rejects the WHOLE authorization dialog when any one scope is not
    # available to the app ("Invalid Scopes: …"), so an optional analytics
    # permission that has not been through App Review takes publishing down with
    # it. Only the permissions publishing genuinely needs are requested by
    # default; the engagement/analytics extras are opt-in via
    # ``INSTAGRAM_SCOPES`` once the app is approved for them.
    REQUIRED_SCOPES = (
        "instagram_basic",
        "instagram_content_publish",
        "pages_show_list",
        "pages_read_engagement",
        "business_management",
    )
    # Engagement/analytics. Both need App Review before an app may ask for them,
    # and `instagram_manage_insights` is the one that has actually blocked
    # connections in the wild ("Invalid Scopes: instagram_manage_insights") —
    # it is in the default set, so an app without it must strip it back out via
    # INSTAGRAM_SCOPES until App Review grants it.
    OPTIONAL_SCOPES = (
        "instagram_manage_comments",   # reply/hide/delete comments
        "instagram_manage_insights",   # media + account metrics
    )
    DEFAULT_SCOPES = REQUIRED_SCOPES + OPTIONAL_SCOPES

    @property
    def scopes(self) -> list[str]:  # type: ignore[override]
        """What to ask for, honouring ``INSTAGRAM_SCOPES`` when it is set.

        The env var replaces the list outright, so an app approved for insights
        can request them and an app in trouble can strip back to the minimum.
        """
        from ..config import settings

        override = (settings.instagram_scopes or "").strip()
        if override:
            return [s.strip() for s in re.split(r"[,\s]+", override) if s.strip()]
        return list(self.DEFAULT_SCOPES)
    scope_sep = ","

    async def fetch_identity(self, access_token: str) -> Identity:
        """Resolve the IG business account + its Page access token from a user token.

        Publishing acts AS THE PAGE, so what gets stored has to be the page's own
        token. Graph only returns one in ``/me/accounts`` when the login actually
        granted the page permissions; a page with no ``access_token`` field means
        it did not, and falling back to the user token here is what produced

            Any of the pages_read_engagement, pages_manage_metadata, … permission(s)
            must be granted before impersonating a user's page. [code=190]

        at publish time, minutes and one generated image later. Better to refuse
        the connection now and say which step of the dialog to redo.
        """
        pages = await self._list_pages(access_token)
        linked = [p for p in pages if p.get("instagram_business_account")]
        if not linked:
            raise RuntimeError(
                f"No Instagram Business account found on any Facebook Page for this login "
                f"({len(pages)} page(s) visible). Link an IG Business/Creator account to a "
                f"Page, and make sure you ticked that Page in the login dialog."
            )

        page = linked[0]
        page_token = page.get("access_token", "")
        if not page_token:
            raise RuntimeError(
                f"Facebook returned the Page '{page.get('name', '?')}' without a page access "
                f"token, so this connection could publish nothing. That happens when the "
                f"login did not grant page access: on the 'What do you want to allow' step, "
                f"make sure the PAGE itself is ticked (not only the Instagram account) and "
                f"that pages_show_list / pages_read_engagement are left enabled. "
                f"Disconnect and connect again."
            )

        iba = page["instagram_business_account"]
        logger.info("Instagram connected: @%s via Page '%s'",
                    iba.get("username", "?"), page.get("name", "?"))
        return Identity(
            external_id=iba["id"],
            handle=iba.get("username") or page.get("name", ""),
            # Publishing uses the PAGE access token; store it as the account token.
            meta={"access_token": page_token, "page_name": page.get("name", "")},
        )

    async def fetch_identities(self, access_token: str) -> list[Identity]:
        """Every Instagram account this login administers, in one go.

        A Meta app holds ONE grant per Facebook user, so connecting accounts one
        at a time makes each authorization replace the last — and the page tokens
        minted by earlier ones stop working. Claiming every linked Page from a
        single authorization sidesteps that completely.

        A Page that came back without a token is skipped with a warning rather
        than failing the whole connect: the other Pages are still usable, and the
        one that isn't gets reported by the accounts page's permission check.
        """
        pages = await self._list_pages(access_token)
        identities = []
        for page in pages:
            iba = page.get("instagram_business_account")
            if not iba:
                continue
            token = page.get("access_token", "")
            if not token:
                logger.warning("Page '%s' (@%s) has no page access token — skipping it. "
                               "Re-run the login and tick that Page.",
                               page.get("name", "?"), iba.get("username", "?"))
                continue
            identities.append(Identity(
                external_id=iba["id"],
                handle=iba.get("username") or page.get("name", ""),
                meta={"access_token": token, "page_name": page.get("name", "")},
            ))

        if not identities:
            # Reuse the single-account path purely for its diagnostic messages.
            await self.fetch_identity(access_token)
        logger.info("Instagram login covers %d account(s): %s", len(identities),
                    ", ".join("@" + i.handle for i in identities))
        return identities

    async def _list_pages(self, access_token: str) -> list[dict]:
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
            return resp.json().get("data", [])

    async def inspect_token(self, access_token: str, account=None) -> dict:
        """What this stored token actually IS, straight from Graph.

        ``/debug_token`` answers the two questions logs cannot. **Is it a PAGE
        token?** — publishing acts as the Page, and a USER token here produces
        `code=190 … impersonating a user's page` however complete its scope list
        looks. **What was really granted?** — the dialog lets a user change which
        Pages an app may use, and re-authorising with a different selection
        silently drops the ones left unticked.

        Returns ``{}`` when it cannot tell (no app credentials, network trouble),
        never raises: it is a diagnostic and must not be able to break a page.
        """
        creds = getattr(self, "creds", None)
        if not (creds and creds.configured):
            return {}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{GRAPH}/debug_token",
                    params={"input_token": access_token,
                            "access_token": f"{creds.client_id}|{creds.client_secret}"})
            if r.status_code >= 400:
                logger.info("Could not inspect the token: HTTP %s", r.status_code)
                return {}
            data = r.json().get("data", {}) or {}
            return {
                "type": (data.get("type") or "").upper(),      # USER | PAGE
                "scopes": list(data.get("scopes", [])),
                "is_valid": bool(data.get("is_valid", False)),
                "profile_id": str(data.get("profile_id", "")),
                "expires_at": data.get("expires_at", 0),
            }
        except Exception as exc:  # noqa: BLE001 - diagnostics must never break a page
            logger.warning("Token inspection failed: %s", exc)
            return {}

    async def granted_scopes(self, access_token: str) -> list[str]:
        """Just the permission list — kept for callers that only need that."""
        return (await self.inspect_token(access_token)).get("scopes", [])

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
            if err.get("code") in _RATE_LIMIT_CODES or \
                    err.get("error_subcode") in _BLOCKED_SUBCODES:
                raise RateLimited(message)          # never retry a volume refusal
            if err.get("code") in _NOT_READY_CODES and attempt < _PUBLISH_RETRIES - 1:
                logger.info("Instagram media not publishable yet (attempt %d/%d): %s",
                            attempt + 1, _PUBLISH_RETRIES, message)
                await asyncio.sleep(_PUBLISH_RETRY_DELAY)
                continue
            raise RuntimeError(message)
        raise RuntimeError("Instagram media never became publishable")

    async def _find_recent_published(self, client, ig_user_id, token, caption,
                                     within_seconds=_RECONCILE_WINDOW_SECONDS) -> dict | None:
        """Did a post we just tried to publish actually land? Never raises.

        ``media_publish`` can fail *after* the container reached FINISHED — Meta
        already holds the media, and a code-4 refusal at that last step leaves the
        outcome genuinely UNKNOWN rather than failed. Assuming failure is what
        produced duplicate posts: the run reported failure, the position stayed
        put, and the next run posted the same thing again.

        So we ask the account what its newest post is. A post carrying our caption
        and timestamped within the last few minutes is the one we just submitted.

        Requires a caption to match on. ``/media`` does not list stories, so
        without one the newest *feed* post would be misread as ours — the
        [publish ledger](../publish_ledger.py) is what guards story duplicates.
        """
        wanted = _caption_key(caption)
        if not wanted:
            logger.info("No caption to reconcile against; reporting the publish failure as-is")
            return None
        try:
            r = await client.get(f"{GRAPH}/{ig_user_id}/media",
                                 params={"fields": "id,caption,permalink,timestamp",
                                         "limit": 5},
                                 headers=_auth(token))
            if r.status_code >= 400:
                logger.info("Could not reconcile the publish (HTTP %s reading recent media)",
                            r.status_code)
                return None
            recent = r.json().get("data", [])
        except Exception as exc:  # noqa: BLE001 - reconciliation is best-effort
            logger.warning("Could not reconcile the publish: %s", exc)
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
        for post in recent:
            stamp = _parse_graph_time(post.get("timestamp"))
            if stamp is None or stamp < cutoff:
                continue
            if _caption_key(post.get("caption")) == wanted:
                return post
        return None

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

    def _public_media_url(self, asset_path: str) -> str:
        media_url = public_url(asset_path)
        if not media_url:
            raise RuntimeError("Instagram needs a media asset; generate an image or reel first.")
        if media_url.startswith(("http://127.", "http://localhost")):
            raise RuntimeError(
                "Instagram must fetch media from a PUBLIC url. Set DASHBOARD_BASE_URL to a "
                "public https address (e.g. an ngrok tunnel) so /assets/<file> is reachable."
            )
        return media_url

    async def _create_container(self, client, ig_user_id, token, data) -> str:
        r = await client.post(f"{GRAPH}/{ig_user_id}/media", data=data, headers=_auth(token))
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_graph(exc)
        return r.json()["id"]

    async def _create_carousel_container(self, client, ig_user_id, token, caption,
                                         asset_paths, media_kinds, extra=None) -> str:
        """Child container per item, then one CAROUSEL parent listing their ids.

        Children carry ``is_carousel_item=true`` and NO caption — the caption
        belongs to the parent. Video children use ``media_type=VIDEO`` (not
        ``REELS``, which is a standalone placement).

        ``extra`` (the AI-generated flag) goes on the PARENT only: Meta documents
        ``is_ai_generated`` as a property of the post, and the children are not
        posts.
        """
        children = []
        for path, kind in zip(asset_paths, media_kinds):
            url = self._public_media_url(path)
            await self._log_media_preflight(client, url, path, kind)
            data = {"is_carousel_item": "true"}
            if kind == "video":
                data.update({"media_type": "VIDEO", "video_url": url})
            else:
                data["image_url"] = url
            child_id = await self._create_container(client, ig_user_id, token, data)
            await self._wait_finished(client, child_id, token,
                                      tries=30 if kind == "video" else 10)
            children.append(child_id)
            logger.info("Instagram carousel item %d/%d ready (%s)",
                        len(children), len(asset_paths), child_id)

        return await self._create_container(client, ig_user_id, token, {
            "media_type": "CAROUSEL", "children": ",".join(children), "caption": caption,
            **(extra or {})})

    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind,
                      instruction=None, asset_paths=None, placement="feed") -> PublishResult:
        ig_user_id = account.external_id
        paths = [p for p in (asset_paths or [asset_path]) if p]
        if not paths:
            raise RuntimeError("Instagram needs a media asset; generate an image or reel first.")
        kinds = [kind_from_path(p) for p in paths]
        placement = (placement or "feed").lower()

        # Meta's own "AI info" label — the same switch the app shows as "Add AI
        # Label" in the composer. A rendered platform label, not a line of prose
        # at the end of the caption. Graph wants the string "true".
        ai_flags = {key: "true" for key in disclosure.native_flags("instagram", instruction)}

        async with httpx.AsyncClient(timeout=120) as client:
            # Meta's processing failures (e.g. the undocumented 2207076) are
            # widely reported as intermittent, so rebuild the container once.
            for attempt in range(_CONTAINER_RETRIES):
                if len(paths) > 1:
                    creation_id = await self._create_carousel_container(
                        client, ig_user_id, access_token, caption, paths, kinds,
                        extra=ai_flags)
                    media_kind, tries = "carousel", 15
                else:
                    media_url = self._public_media_url(paths[0])
                    # Log exactly what Instagram is about to fetch. A container that
                    # errors during PROCESSING gives no clue about the file, so record
                    # the file's own properties before handing over the URL.
                    await self._log_media_preflight(client, media_url, paths[0], kinds[0])
                    if placement == "story":
                        # Stories take no caption — Graph ignores/rejects it.
                        data = {"media_type": "STORIES", **ai_flags}
                        data["video_url" if kinds[0] == "video" else "image_url"] = media_url
                    elif kinds[0] == "video":
                        data = {"caption": caption, "media_type": "REELS",
                                "video_url": media_url, **ai_flags}
                    else:
                        data = {"caption": caption, "image_url": media_url, **ai_flags}
                    creation_id = await self._create_container(
                        client, ig_user_id, access_token, data)
                    tries = 30 if kinds[0] == "video" else 10

                logger.info("Instagram container %s created (%s, placement=%s, %d item(s), "
                            "caption %d chars)", creation_id, media_kind, placement,
                            len(paths), len(caption or ""))

                # `tries` was set per placement above: Reels take minutes to
                # transcode, images are usually instant, and a carousel parent
                # only assembles already-finished children.
                try:
                    await self._wait_finished(client, creation_id, access_token, tries=tries)
                    break
                except ContainerError:
                    if attempt == _CONTAINER_RETRIES - 1:
                        raise
                    logger.warning("Rebuilding the Instagram container after a processing "
                                   "failure (attempt %d/%d)", attempt + 1, _CONTAINER_RETRIES)
                    await asyncio.sleep(_CONTAINER_RETRY_DELAY)

            try:
                media_id = await self._publish_container(
                    client, ig_user_id, creation_id, access_token)
            except (RateLimited, RuntimeError) as exc:
                # The container was FINISHED, so Meta already had the media and may
                # have published it anyway. Check before reporting a failure that
                # would make the next run post this a second time.
                landed = await self._find_recent_published(
                    client, ig_user_id, access_token,
                    "" if placement == "story" else caption)
                if landed is None:
                    raise
                logger.warning("media_publish reported %r, but the post IS live (%s) — "
                               "treating it as published so the next run does not repeat it",
                               str(exc), landed.get("permalink") or landed.get("id"))
                return PublishResult(url=landed.get("permalink", ""),
                                     external_id=landed.get("id", ""),
                                     raw={"media_id": landed.get("id", ""),
                                          "reconciled": True, "publish_error": str(exc)})

            perma = await client.get(f"{GRAPH}/{media_id}", params={"fields": "permalink"},
                                     headers=_auth(access_token))
            url = perma.json().get("permalink", "") if perma.status_code == 200 else ""
        logger.info("Instagram published media %s", media_id)
        return PublishResult(url=url, external_id=media_id, raw={"media_id": media_id})


    # ------------------------------------------------------------------ #
    # Reading and engagement
    #
    # These are what turn the agent from a publisher into a manager: it can see
    # what it already posted (so it doesn't repeat itself), read and answer
    # comments, check how a post performed, and — importantly — check the
    # publishing quota BEFORE spending a Sora clip on a post that cannot be
    # published for another 20 hours.
    # ------------------------------------------------------------------ #
    async def _graph_get(self, access_token: str, path: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{GRAPH}/{path}", params=params, headers=_auth(access_token))
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise_graph(exc)
            return r.json()

    async def _graph_post(self, access_token: str, path: str, data: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{GRAPH}/{path}", data=data, headers=_auth(access_token))
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise_graph(exc)
            return r.json()

    MEDIA_FIELDS = ("id,caption,media_type,media_product_type,permalink,timestamp,"
                    "like_count,comments_count,thumbnail_url,media_url")
    COMMENT_FIELDS = ("id,text,username,timestamp,like_count,hidden,"
                      "replies{id,text,username,timestamp}")
    # Metric names churn (v21 deprecated video_views for non-Reels, profile_views,
    # website_clicks…), so keep the default set small and let callers override.
    DEFAULT_MEDIA_METRICS = "reach,likes,comments,saved,shares"
    DEFAULT_ACCOUNT_METRICS = "reach,follower_count"

    async def post_exists(self, access_token: str, account: Account,
                          external_id: str) -> bool | None:
        """Is this media still on the profile grid — not deleted, not archived?

        Two signals, because Graph offers no "is_archived" field:

        1. ``GET /{media-id}`` — a DELETED post answers with code 803 / subcode 33.
           An archived one still resolves, so this alone cannot see archiving.
        2. ``GET /{ig-user-id}/media`` — the profile listing **excludes archived
           posts**. A media id that resolves but is absent from the listing has
           been taken off the grid.

        The trap in signal 2 is paging: the listing is one bounded page, so an old
        post that is still live simply isn't in it. Absence therefore only means
        "archived" when the post is NEWER than the oldest row we actually looked
        at — otherwise we never covered its date and the honest answer is ``None``.

        Anything else (rate limit, network, token trouble) is ``None`` too, so the
        caller decides what an unknown means rather than this guessing.
        """
        if not external_id:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{GRAPH}/{external_id}",
                                     params={"fields": "id,timestamp"},
                                     headers=_auth(access_token))
                if r.status_code >= 400:
                    exc = httpx.HTTPStatusError("lookup failed", request=r.request,
                                                response=r)
                    message, err = _graph_error(exc)
                    if (err.get("code") in _GONE_CODES
                            or err.get("error_subcode") in _GONE_SUBCODES):
                        logger.info("Instagram media %s was deleted (%s)",
                                    external_id, message)
                        return False
                    logger.info("Could not determine whether media %s still exists: %s",
                                external_id, message)
                    return None
                posted_at = _parse_graph_time(r.json().get("timestamp"))

                listing = await client.get(
                    f"{GRAPH}/{account.external_id}/media",
                    params={"fields": "id,timestamp", "limit": _GRID_SCAN_LIMIT},
                    headers=_auth(access_token))
            if listing.status_code >= 400:
                logger.info("Media exists but the profile listing is unreadable (HTTP %s); "
                            "treating %s as still live", listing.status_code, external_id)
                return True

            rows = listing.json().get("data", [])
            if any(row.get("id") == external_id for row in rows):
                return True

            stamps = [s for s in (_parse_graph_time(row.get("timestamp")) for row in rows) if s]
            oldest = min(stamps) if stamps else None
            if posted_at and oldest and posted_at >= oldest:
                # Inside the window we scanned, yet not on the grid -> archived.
                logger.info("Instagram media %s resolves but is absent from the profile "
                            "listing — treating it as ARCHIVED (not live)", external_id)
                return False
            logger.info("Media %s is older than the %d posts scanned; cannot tell whether "
                        "it is archived", external_id, len(rows))
            return None
        except Exception as exc:  # noqa: BLE001 - never fail a publish over this check
            logger.warning("Media existence check for %s failed: %s", external_id, exc)
            return None

    async def list_media(self, access_token: str, account: Account, *, limit: int = 10,
                         fields: str = "") -> list[dict]:
        """Recent posts on this account, with their captions and counts.

        **Pages.** Graph caps one response at 100 regardless of what you ask for,
        so a request for 200 used to come back with 100 and no indication that
        anything was missing — which silently truncates work that walks back
        through an arc ("every post since the intro"). Follow ``paging.next``
        until the caller's limit is met or the account runs out.
        """
        wanted = max(1, min(int(limit or 10), MAX_MEDIA_PAGE_TOTAL))
        collected: list[dict] = []
        params = {"fields": fields or self.MEDIA_FIELDS, "limit": min(wanted, 100)}
        path = f"{account.external_id}/media"
        while True:
            payload = await self._graph_get(access_token, path, params)
            collected.extend(payload.get("data") or [])
            if len(collected) >= wanted:
                break
            after = (((payload.get("paging") or {}).get("cursors") or {}).get("after") or "")
            if not after or not (payload.get("paging") or {}).get("next"):
                break
            params = {**params, "after": after,
                      "limit": min(wanted - len(collected), 100)}
        return collected[:wanted]

    async def list_comments(self, access_token: str, media_id: str, *,
                            limit: int = 25) -> list[dict]:
        """Comments on one post, with their replies."""
        payload = await self._graph_get(access_token, f"{media_id}/comments", {
            "fields": self.COMMENT_FIELDS, "limit": max(1, min(limit, 100))})
        return payload.get("data", [])

    async def reply_to_comment(self, access_token: str, comment_id: str, message: str) -> dict:
        """Post a public reply under a comment."""
        return await self._graph_post(access_token, f"{comment_id}/replies",
                                      {"message": message})

    async def set_comment_hidden(self, access_token: str, comment_id: str,
                                 hidden: bool = True) -> dict:
        """Hide (or unhide) a comment — moderation without deleting it."""
        return await self._graph_post(access_token, comment_id,
                                      {"hide": "true" if hidden else "false"})

    async def delete_comment(self, access_token: str, comment_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.delete(f"{GRAPH}/{comment_id}", headers=_auth(access_token))
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise_graph(exc)
            return r.json()

    async def media_insights(self, access_token: str, media_id: str,
                             metrics: str = "") -> list[dict]:
        payload = await self._graph_get(access_token, f"{media_id}/insights", {
            "metric": metrics or self.DEFAULT_MEDIA_METRICS})
        return payload.get("data", [])

    async def account_insights(self, access_token: str, account: Account, *,
                               metrics: str = "", period: str = "day") -> list[dict]:
        payload = await self._graph_get(access_token, f"{account.external_id}/insights", {
            "metric": metrics or self.DEFAULT_ACCOUNT_METRICS, "period": period,
            "metric_type": "total_value"})
        return payload.get("data", [])

    async def account_profile(self, access_token: str, account: Account) -> dict:
        return await self._graph_get(access_token, account.external_id, {
            "fields": "id,username,name,biography,followers_count,follows_count,media_count,"
                      "profile_picture_url"})

    async def publishing_limit(self, access_token: str, account: Account) -> dict:
        """How much of the rolling 24h publishing quota is already used."""
        payload = await self._graph_get(
            access_token, f"{account.external_id}/content_publishing_limit",
            {"fields": "config,quota_usage"})
        rows = payload.get("data", [])
        return rows[0] if rows else {}

    async def list_mentions(self, access_token: str, account: Account, *,
                            limit: int = 10) -> list[dict]:
        """Media where this account was tagged — the other half of engagement."""
        payload = await self._graph_get(access_token, f"{account.external_id}/tags", {
            "fields": self.MEDIA_FIELDS, "limit": max(1, min(limit, 50))})
        return payload.get("data", [])


register(PlatformName.instagram, Instagram)
