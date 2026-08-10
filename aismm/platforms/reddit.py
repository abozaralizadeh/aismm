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
import re
from datetime import datetime, timezone

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
        # A submission's score/comment count/upvote ratio are public via
        # /api/info — the feedback loop reads them per post.
        supports_metrics=True,
        # Reddit is a natural OUTREACH surface: /search and /r/{sub}/new find
        # other people's submissions by keyword/subreddit, and /api/comment
        # replies to them. Both the existing `read` and `submit` scopes already
        # cover this, so no reconnect is needed to turn it on.
        supports_search=True,
        supports_comments=True,
        # /message/inbox reads private messages and /api/comment answers them —
        # both need the `privatemessages` scope below, so an account connected
        # before it was added must be reconnected.
        supports_dms=True,
    )
    auth_endpoint = f"{WWW}/api/v1/authorize"
    token_endpoint = f"{WWW}/api/v1/access_token"
    scopes = ["identity", "submit", "read", "privatemessages"]
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

    async def fetch_post_metrics(self, access_token: str, account: Account, *,
                                 external_id: str) -> dict | None:
        """Normalized score/comment/upvote-ratio for one submission.

        ``/api/info?id=t3_<id>`` returns the listing wrapper; the submission's
        ``data`` carries ``score``, ``num_comments`` and ``upvote_ratio``. The
        stored ``external_id`` is the bare id, so re-add the ``t3_`` fullname
        prefix. Returns ``None`` on any failure so one bad post never stops a sweep.
        """
        if not external_id:
            return None
        fullname = external_id if external_id.startswith("t3_") else f"t3_{external_id}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{OAUTH}/api/info",
                                     params={"id": fullname}, headers=_headers(access_token))
                r.raise_for_status()
                children = (r.json().get("data") or {}).get("children") or []
        except Exception as exc:  # noqa: BLE001 - one bad post must not stop the sweep
            logger.warning("Reddit metrics for %s failed: %s", external_id, exc)
            return None
        if not children:
            return {}
        data = children[0].get("data") or {}
        return {
            "score": data.get("score", 0),
            "comments": data.get("num_comments", 0),
            "upvote_ratio": data.get("upvote_ratio", 0),
        }

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

    # ------------------------------------------------------------------ #
    # Outreach — find other people's submissions and comment on them
    # ------------------------------------------------------------------ #
    async def search_content(self, access_token: str, account: Account, *,
                             query: str, limit: int = 10, subreddit: str = "") -> list[dict]:
        """Recent submissions from OTHER redditors to engage — the outreach read.

        Three shapes, picked by what the tool passes:

        * ``subreddit`` + ``query`` -> ``/r/{sub}/search`` restricted to that sub;
        * ``subreddit`` only        -> ``/r/{sub}/new`` (the sub's latest posts);
        * ``query`` only            -> a site-wide ``/search`` for links.

        Sorted newest-first, self-posts and NSFW submissions dropped (an outreach
        bot should never auto-reply under either). Returns normalized items whose
        ``id`` is the ``t3_`` fullname to pass straight to :meth:`reply_to_target`.
        Best-effort: a failed search returns ``[]`` rather than killing the run.
        """
        sub = (subreddit or "").removeprefix("/r/").removeprefix("r/").strip("/")
        q = (query or "").strip()
        n = max(1, min(limit, 100))
        if sub and q:
            path, params = f"r/{sub}/search", {
                "q": q, "restrict_sr": "1", "sort": "new", "limit": n, "type": "link"}
        elif sub:
            path, params = f"r/{sub}/new", {"limit": n}
        elif q:
            path, params = "search", {"q": q, "sort": "new", "limit": n, "type": "link"}
        else:
            return []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{OAUTH}/{path}", params=params,
                                     headers=_headers(access_token))
                r.raise_for_status()
                children = (r.json().get("data") or {}).get("children") or []
        except Exception as exc:  # noqa: BLE001 - outreach search is best-effort
            logger.warning("Reddit search (%s) failed: %s", path, exc)
            return []
        me = (account.handle or "").lstrip("@").lower()
        items: list[dict] = []
        for child in children:
            data = child.get("data") or {}
            if child.get("kind") != "t3":       # only submissions here
                continue
            if data.get("over_18"):             # never auto-engage NSFW
                continue
            author = (data.get("author") or "").lstrip("u/")
            if me and author.lower() == me:     # never engage our own post
                continue
            fullname = data.get("name") or (f"t3_{data.get('id')}" if data.get("id") else "")
            if not fullname:
                continue
            title = data.get("title") or ""
            body = (data.get("selftext") or "").strip()
            text = f"{title}\n\n{body}".strip() if body else title
            items.append({
                "id": fullname,
                "text": text[:600],
                "url": "https://www.reddit.com" + (data.get("permalink") or ""),
                "author": f"u/{author}" if author else "",
                "posted_at": _epoch_iso(data.get("created_utc")),
                "subreddit": f"r/{data.get('subreddit')}" if data.get("subreddit") else "",
                "score": data.get("score"),
                "comments": data.get("num_comments"),
            })
        return items[:limit]

    async def list_dms(self, access_token: str, account: Account, *,
                       limit: int = 25) -> list[dict]:
        """Recent INBOUND private messages awaiting a reply.

        ``GET /message/inbox`` returns a Listing of ``t4`` things (private
        messages) newest first. Reddit also drops COMMENT replies into the same
        inbox (``was_comment=True``); those belong to the comment-engagement path,
        so only true PMs are kept here. Messages this account itself authored are
        excluded so it never answers itself. ``id`` is the message fullname
        (``t4_…``) — both the ledger dedupe key AND the ``thing_id`` a reply is
        addressed to, so no separate conversation id is needed. Needs the
        ``privatemessages`` scope. Best-effort: a failure returns ``[]``.
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{OAUTH}/message/inbox",
                                     params={"limit": max(1, min(limit, 100))},
                                     headers=_headers(access_token))
                r.raise_for_status()
                children = (r.json().get("data") or {}).get("children") or []
        except Exception as exc:  # noqa: BLE001 - DM read is best-effort
            logger.warning("Reddit inbox read failed: %s", exc)
            return []
        me = (account.handle or "").lstrip("@").lower().removeprefix("u/")
        items: list[dict] = []
        for child in children:
            data = child.get("data") or {}
            if child.get("kind") != "t4":       # only private messages, not comment replies
                continue
            if data.get("was_comment"):
                continue
            author = (data.get("author") or "").lstrip("u/")
            if me and author.lower() == me:     # never answer our own message
                continue
            fullname = data.get("name") or (f"t4_{data.get('id')}" if data.get("id") else "")
            if not fullname:
                continue
            subject = (data.get("subject") or "").strip()
            body = (data.get("body") or "").strip()
            text = f"{subject}\n\n{body}".strip() if subject else body
            items.append({
                "id": fullname,
                "conversation_id": "",          # Reddit addresses the reply by fullname
                "sender": f"u/{author}" if author else "",
                "sender_id": author,
                "text": text[:1000],
                "created_at": _epoch_iso(data.get("created_utc")),
            })
        return items[:limit]

    async def reply_to_target(self, access_token: str, account: Account, *,
                              target_type: str, target_id: str, text: str,
                              reply_to: str = "") -> dict:
        """Comment on a submission, reply to a comment, or answer a DM (mode-gated).

        ``POST /api/comment`` takes a ``thing_id`` fullname — ``t3_`` for a
        submission, ``t1_`` for a comment, ``t4_`` for a private message — and
        Markdown ``text``. If ``target_id`` arrives without a prefix, one is added
        from ``target_type`` (``comment`` -> ``t1_``, ``dm`` -> ``t4_``, anything
        else -> ``t3_``). A DM's fullname IS the send destination, so ``reply_to``
        is unused. Needs the ``submit`` scope (``privatemessages`` for a DM), which
        the account has. Returns the new comment's id and permalink.
        """
        thing_id = str(target_id or "").strip()
        if not re.match(r"^t[0-9]_", thing_id):
            prefix = {"comment": "t1_", "dm": "t4_"}.get((target_type or "").lower(), "t3_")
            thing_id = prefix + thing_id
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{OAUTH}/api/comment",
                data={"thing_id": thing_id, "text": text, "api_type": "json"},
                headers=_headers(access_token))
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _raise(exc)
            payload = r.json().get("json", {}) if isinstance(r.json(), dict) else {}
        errors = payload.get("errors") or []
        if errors:
            detail = "; ".join(e[1] if len(e) > 1 else str(e) for e in errors)
            raise RuntimeError(f"Reddit rejected the comment: {detail}")
        things = (payload.get("data") or {}).get("things") or []
        data = things[0].get("data") if things else {}
        data = data or {}
        permalink = data.get("permalink") or ""
        return {"id": data.get("name") or data.get("id") or "",
                "url": ("https://www.reddit.com" + permalink) if permalink else ""}


def _epoch_iso(value) -> str:
    """Reddit's ``created_utc`` (epoch seconds, as a float) -> an ISO string, or ""."""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


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
