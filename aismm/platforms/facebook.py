"""Facebook — Meta Graph API (Facebook **Page** publishing).

Facebook Pages ride the *same* Meta Graph infrastructure as Instagram, so this
module is a thin sibling of :mod:`aismm.platforms.instagram`: it reuses that
module's bearer-auth rule, its ``RateLimited`` type and the volume-refusal codes,
and differs only in the publishing endpoints.

Publishing acts **as the Page**, so — exactly like Instagram — the stored token
must be the Page access token from ``/me/accounts`` (never the user token), and a
login that did not grant page access is refused at connect time rather than
failing later at publish. One login covers every Page it administers
(:meth:`fetch_identities`), which is what stops a second connect from replacing
the first login's grant.

Endpoints (all under a Page, token as ``Authorization: Bearer``):
    * text   →  POST /{page-id}/feed        message
    * image  →  POST /{page-id}/photos      url + caption
    * album  →  POST /{page-id}/photos      url + published=false  (per child)
                then POST /{page-id}/feed   message + attached_media[]
    * video  →  POST /{page-id}/videos      file_url + description

Like Instagram, Facebook FETCHES the media from a public URL, so the asset must be
reachable (``needs_public_media_url=True``).
"""
from __future__ import annotations

import json
import logging

import httpx

from .. import disclosure
from ..assets import public_url
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .instagram import (
    GRAPH,
    GRAPH_VERSION,
    RateLimited,
    _auth,
    _safe_url,
    _BLOCKED_SUBCODES,
    _RATE_LIMIT_CODES,
)
from .registry import register

logger = logging.getLogger("aismm.platforms.facebook")


def _graph_error(exc: httpx.HTTPStatusError) -> tuple[str, dict]:
    """Facebook-flavoured Graph error message + its error dict (token-safe)."""
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
        return f"Facebook Graph {resp.status_code} at {_safe_url(resp)}: {body}", {}
    detail = " · ".join(
        f"{key}={err[key]}" for key in
        ("code", "error_subcode", "type", "error_user_title", "error_user_msg", "fbtrace_id")
        if err.get(key) not in (None, "")
    )
    message = err.get("message") or "no message"
    return (f"Facebook Graph {resp.status_code}: {message}"
            + (f" [{detail}]" if detail else "")), err


def _raise_graph(exc: httpx.HTTPStatusError) -> None:
    """Re-raise a Graph failure with the body included; volume refusals as RateLimited."""
    message, err = _graph_error(exc)
    if err.get("code") in _RATE_LIMIT_CODES or err.get("error_subcode") in _BLOCKED_SUBCODES:
        raise RateLimited(message) from None
    raise RuntimeError(message) from None


class Facebook(SocialPlatform):
    name = PlatformName.facebook
    capabilities = Capabilities(
        supports_text=True,            # a Page can post text with no media
        supports_image=True,
        supports_video=True,
        needs_public_media_url=True,   # Graph fetches the media from a URL
        default_orientation="landscape",
        caption_limit=63206,           # Facebook's post character ceiling
        notes="Posts to a Facebook Page. Media is fetched from a public URL. "
              "Up to 10 photos publish as a single album post.",
        supports_carousel=True,        # multi-photo album
        max_carousel_items=10,
        # A Page post exposes like/comment/share counts (the Page's own token can
        # read the .summary edges), so the feedback loop can poll them per post.
        supports_metrics=True,
    )
    auth_endpoint = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
    token_endpoint = f"{GRAPH}/oauth/access_token"
    # Publishing to a Page needs the Page in the login (pages_show_list /
    # pages_read_engagement) plus permission to post (pages_manage_posts).
    scopes = [
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
        "business_management",
    ]
    scope_sep = ","

    # ------------------------------------------------------------------ #
    # Identity — the Page and its page token
    # ------------------------------------------------------------------ #
    async def _list_pages(self, access_token: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{GRAPH}/me/accounts",
                params={"fields": "id,name,access_token"},
                headers=_auth(access_token),
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise_graph(exc)
            return resp.json().get("data", [])

    async def fetch_identity(self, access_token: str) -> Identity:
        """Resolve the first Page + its page token from a user token."""
        identities = await self.fetch_identities(access_token)
        return identities[0]

    async def fetch_identities(self, access_token: str) -> list[Identity]:
        """Every Facebook Page this login administers, each with its page token.

        A Meta app holds ONE grant per Facebook user, so — exactly as for
        Instagram — connecting Pages one at a time makes each authorization
        replace the last. Claiming every Page from a single login avoids that.
        """
        pages = await self._list_pages(access_token)
        identities = []
        for page in pages:
            token = page.get("access_token", "")
            if not token:
                logger.warning("Page '%s' has no page access token — skipping it. "
                               "Re-run the login and tick that Page.", page.get("name", "?"))
                continue
            identities.append(Identity(
                external_id=page["id"],
                handle=page.get("name", ""),
                meta={"access_token": token, "page_name": page.get("name", "")},
            ))
        if not identities:
            raise RuntimeError(
                f"No Facebook Page returned a page access token for this login "
                f"({len(pages)} page(s) visible). On the 'What do you want to allow' step, "
                f"tick the PAGE itself (not only your profile) and leave pages_show_list / "
                f"pages_manage_posts enabled, then connect again.")
        logger.info("Facebook login covers %d page(s): %s", len(identities),
                    ", ".join(i.handle for i in identities))
        return identities

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #
    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind,
                      instruction=None, asset_paths=None, placement="feed") -> PublishResult:
        page_id = account.external_id
        paths = [p for p in (asset_paths or ([asset_path] if asset_path else [])) if p]
        # Meta has no post-time AI flag on the Page feed/photo/video endpoints the
        # way an IG container does, so disclosure rides the (opt-in) caption suffix.
        _ = disclosure.native_flags("facebook", instruction)

        async with httpx.AsyncClient(timeout=120) as client:
            if media_kind == "video":
                if len(paths) > 1:
                    raise RuntimeError("Facebook takes one video per post; publish separately.")
                return await self._post_video(client, page_id, access_token, paths[0], caption)
            if media_kind == "image":
                if not paths:
                    raise RuntimeError("Facebook image post has no media asset.")
                if len(paths) == 1:
                    return await self._post_photo(client, page_id, access_token, paths[0], caption)
                return await self._post_album(client, page_id, access_token, paths, caption)
            # Text-only status update.
            return await self._post_text(client, page_id, access_token, caption)

    async def _graph_post(self, client, path: str, token: str, data: dict) -> dict:
        r = await client.post(f"{GRAPH}/{path}", data=data, headers=_auth(token))
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_graph(exc)
        return r.json()

    async def _post_text(self, client, page_id, token, caption) -> PublishResult:
        body = await self._graph_post(client, f"{page_id}/feed", token, {"message": caption or ""})
        post_id = body.get("id", "")
        return PublishResult(url=_post_url(post_id), external_id=post_id, raw=body)

    async def _post_photo(self, client, page_id, token, path, caption) -> PublishResult:
        body = await self._graph_post(client, f"{page_id}/photos", token,
                                      {"url": public_url(path), "caption": caption or ""})
        post_id = body.get("post_id") or body.get("id", "")
        return PublishResult(url=_post_url(post_id), external_id=post_id, raw=body)

    async def _post_album(self, client, page_id, token, paths, caption) -> PublishResult:
        media_fbids = []
        for path in paths[:self.capabilities.max_carousel_items]:
            child = await self._graph_post(client, f"{page_id}/photos", token,
                                           {"url": public_url(path), "published": "false"})
            if child.get("id"):
                media_fbids.append(child["id"])
        data = {"message": caption or ""}
        for i, fbid in enumerate(media_fbids):
            data[f"attached_media[{i}]"] = json.dumps({"media_fbid": fbid})
        body = await self._graph_post(client, f"{page_id}/feed", token, data)
        post_id = body.get("id", "")
        return PublishResult(url=_post_url(post_id), external_id=post_id, raw=body)

    async def _post_video(self, client, page_id, token, path, caption) -> PublishResult:
        body = await self._graph_post(client, f"{page_id}/videos", token,
                                      {"file_url": public_url(path), "description": caption or ""})
        video_id = body.get("id", "")
        url = f"https://www.facebook.com/watch/?v={video_id}" if video_id else ""
        return PublishResult(url=url, external_id=video_id, raw=body)

    async def fetch_post_metrics(self, access_token: str, account: Account, *,
                                 external_id: str) -> dict | None:
        """Normalized like/comment/share counts for one Page post.

        The ``.summary(true)`` edges return the totals without listing every
        like/comment; ``shares`` is a plain object with a ``count``. Read with the
        stored PAGE token. Returns ``None`` on any failure so a single unreadable
        post never breaks the sweep.
        """
        if not external_id:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{GRAPH}/{external_id}",
                    params={"fields": "likes.summary(true),comments.summary(true),shares"},
                    headers=_auth(access_token))
                r.raise_for_status()
                body = r.json()
        except Exception as exc:  # noqa: BLE001 - one bad post must not stop the sweep
            logger.warning("Facebook metrics for %s failed: %s", external_id, exc)
            return None
        likes = (body.get("likes") or {}).get("summary", {}).get("total_count", 0)
        comments = (body.get("comments") or {}).get("summary", {}).get("total_count", 0)
        shares = (body.get("shares") or {}).get("count", 0)
        return {"likes": likes, "comments": comments, "shares": shares}


def _post_url(post_id: str) -> str:
    """A Page post id is ``{page}_{post}``; the second half is the shareable URL."""
    if not post_id:
        return ""
    tail = post_id.split("_")[-1]
    return f"https://www.facebook.com/{tail}"


register(PlatformName.facebook, Facebook)
