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
import logging
import os
import re

import httpx

from .. import disclosure
from ..assets import read_bytes
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

logger = logging.getLogger("aismm.platforms.twitter")

API = "https://api.x.com/2"
_CHUNK = 4 * 1024 * 1024  # <5MB per APPEND
_TRANSIENT_UPLOAD_STATUSES = {502, 503, 504}

# initialize validates media_type against this list, so a PNG announced as
# image/jpeg is a 400 rather than a re-encode. The bytes are the authority —
# same reasoning as browse_tool.sniff_media: a path extension can lie, and
# nothing upstream guarantees the generator wrote a JPEG.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _media_type(data: bytes, path: str, media_kind: str) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in {"jpg", "jpeg"}:
        return "image/jpeg"
    if ext in {"png", "gif", "webp"}:
        return f"image/{ext}"
    if ext in {"mp4", "mov", "webm"}:
        return "video/mp4" if ext == "mov" else f"video/{ext}"
    return "video/mp4" if media_kind == "video" else "image/jpeg"


def split_thread(text: str, limit: int, max_posts: int, pin_suffix: str = "") -> list[str]:
    """Break a long caption into posts that each fit ``limit``.

    Splits on the largest natural boundary that works — paragraph, then sentence,
    then word — so a thought is never cut mid-word and rarely mid-sentence.
    Numbered ``n/m`` once there is more than one post, which is the X convention
    and tells a reader how much is coming; the counter is inside the limit, not
    added on top of it.

    ``pin_suffix`` is moved from the end of the text to the FIRST post. That is
    for the AI-disclosure label: it is appended to the caption, so on a thread it
    would otherwise land on the last post — invisible to anyone who only sees
    post 1 in their timeline, which is exactly the "first exposure" the label
    exists to cover.

    The text is truncated only if it cannot fit in ``max_posts`` — a bound that
    exists so a runaway caption can't post fifty times.
    """
    text = (text or "").strip()
    if not text:
        return []

    pin = (pin_suffix or "").strip()
    if pin and text.endswith(pin):
        text = text[: -len(pin)].strip()
        if not text:
            return [pin]
    else:
        pin = ""

    if len(text) + (len(pin) + 2 if pin else 0) <= limit:
        return [f"{text}\n\n{pin}" if pin else text]

    # " 99/99" — reserved up front so numbering can never push a post over.
    counter_room = 7
    budget = max(limit - counter_room, 40)
    first_budget = max(budget - (len(pin) + 2 if pin else 0), 40)

    parts: list[str] = []
    remaining = text
    while remaining and len(parts) < max_posts:
        # Post 1 gives up room to the pinned label.
        room = first_budget if not parts else budget
        if len(remaining) <= room:
            parts.append(remaining)
            break
        window = remaining[: room + 1]
        # Prefer a paragraph break, then a sentence end, then any whitespace.
        floor = room // 3
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < floor:
            sentence = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
            cut = sentence + 1 if sentence >= floor else -1
        if cut < floor:
            space = window.rfind(" ")
            cut = space if space > 0 else room
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if pin:
        parts[0] = f"{parts[0]}\n\n{pin}"
    if len(parts) == 1:
        return parts
    total = len(parts)
    return [f"{part} {index}/{total}" for index, part in enumerate(parts, start=1)]


def community_ids(account) -> list[str]:
    """Every community this account posts to, in rotation order.

    ``community_ids`` is the list; ``community_id`` is the single-value form
    earlier versions stored, still honoured so an existing connection keeps
    working untouched.
    """
    meta = account.meta or {}
    listed = meta.get("community_ids")
    if isinstance(listed, str):
        listed = parse_community_ids(listed)
    ids = [str(c).strip() for c in (listed or []) if str(c).strip()]
    if not ids:
        single = str(meta.get("community_id", "")).strip()
        ids = [single] if single else []
    return ids


def parse_community_entries(raw: str) -> list[tuple[str, str]]:
    """``[(id, name), …]`` from what the operator typed, in order, no repeats.

    Accepts a bare list of ids (``123, 456``) and ids LABELLED by hand
    (``123 = AI Builders``). The label matters because an id tells nobody
    anything: X can usually resolve the name itself, but not for every app tier
    or for a community the account has not joined, and typing it is better than
    a page full of 19-digit numbers.

    The rule is ONE ENTRY PER LINE once a line is labelled: a name may contain
    commas ("AI, Robotics & Agents"), so a labelled line runs to its end. Commas
    and semicolons still separate bare ids, which is what the box used to accept.
    """
    seen: set[str] = set()
    entries: list[tuple[str, str]] = []

    def add(cid: str, name: str = "") -> None:
        cid = cid.strip()
        if cid and cid not in seen:
            seen.add(cid)
            entries.append((cid, name.strip()))

    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" in line:
            cid, _, name = line.partition("=")
            add(cid, name)
            continue
        for token in re.split(r"[\s,;]+", line):
            add(token)
    return entries


def parse_community_ids(raw: str) -> list[str]:
    """Just the ids of :func:`parse_community_entries`."""
    return [cid for cid, _name in parse_community_entries(raw)]


#: An instruction that deliberately posts to the home timeline, rather than one
#: that has simply not been given a destination of its own.
HOME_TIMELINE = "none"


def community_names(account) -> dict:
    """``{id: name}`` for the communities this account knows about.

    Names are resolved once, when the operator saves the destination, and cached
    here — an id is not something anyone recognises, and X charges per call.
    """
    catalog = (account.meta or {}).get("community_names") or {}
    return {str(k): str(v) for k, v in catalog.items() if str(v).strip()}


def community_label(account, community_id: str) -> str:
    """A community's name if we know it, else the bare id."""
    cid = str(community_id or "").strip()
    if not cid:
        return ""
    return community_names(account).get(cid) or cid


def next_community(account, instruction=None) -> str:
    """The community THIS post goes to. Empty string means the home timeline.

    An INSTRUCTION may pin its own destination, and that wins: one brand account
    often has one instruction feeding a niche community and another posting to
    the timeline, which a single account-wide setting cannot express.
    ``twitter_community_id`` is ``""`` to inherit the account's rotation,
    ``HOME_TIMELINE`` to force the timeline, or one community id.

    Inheriting means rotation, not fan-out: one post per run, to the next
    community in the list. Posting the same content to every community at once is
    several near-identical posts from one account within seconds, which is what
    X's duplicate-content rule describes — and it would multiply the cost of a
    pay-per-use API. A scheduler posting fresh content on a cadence covers every
    community anyway, with a genuinely different post each time.
    """
    pinned = str(getattr(instruction, "twitter_community_id", "") or "").strip()
    if pinned == HOME_TIMELINE:
        return ""
    if pinned:
        return pinned

    ids = community_ids(account)
    if not ids:
        return ""
    try:
        cursor = int((account.meta or {}).get("community_cursor", 0))
    except (TypeError, ValueError):
        cursor = 0
    return ids[cursor % len(ids)]


def shares_with_followers(account, instruction=None) -> bool:
    """Whether to set X's "Also share with followers" on this post.

    Tri-state on the instruction: ``""`` inherits the account's switch, ``"yes"``
    and ``"no"`` override it. The same post can be worth broadcasting from one
    instruction and not from another.
    """
    override = str(getattr(instruction, "twitter_share_with_followers", "") or "").strip().lower()
    if override in {"yes", "1", "true", "on"}:
        return True
    if override in {"no", "0", "false", "off"}:
        return False
    return bool((account.meta or {}).get("share_with_followers"))


# X phrases the outreach-reply refusal a few ways; match on the stable parts so a
# minor wording change doesn't drop us back to the generic "reconnect" hint.
_REPLY_BLOCK_MARKERS = (
    "only allowed to posts where",     # "…replies are only allowed to posts where…"
    "mentioned or is the author",
    "not permitted to reply",
    "not allowed to reply",
)


def _is_reply_permission_block(detail: str) -> bool:
    """Is this 403 X refusing an OUTBOUND reply (vs a token/scope failure)?"""
    text = (detail or "").lower()
    return any(marker in text for marker in _REPLY_BLOCK_MARKERS)


# A post's ``reply_settings`` is the AUTHOR's per-post choice of who may reply:
# ``everyone`` (or unset) is open to anyone; ``following`` / ``mentionedUsers`` /
# ``subscribers`` / ``verified`` restrict it. Replying to a restricted post we are
# not mentioned in is exactly the 403 above — a rule X enforces, not a bug to work
# around — so outreach reads this and only offers OPEN posts to reply to.
_REPLIABLE_SETTINGS = ("", "everyone")


def _is_repliable(reply_settings) -> bool:
    """Can anyone reply to a post with this ``reply_settings`` value?

    ``everyone`` and an absent/empty value (X omits it on open posts) are open;
    every other value restricts replies to a group this account is not in.
    """
    return str(reply_settings or "").strip().lower() in _REPLIABLE_SETTINGS


class Twitter(SocialPlatform):
    name = PlatformName.twitter
    capabilities = Capabilities(
        supports_text=True,
        supports_image=True,
        supports_video=True,
        needs_public_media_url=False,
        default_orientation="landscape",
        caption_limit=280,
        notes="280 chars per post, auto-threaded beyond that. "
              "Up to 4 images OR 1 video. Media via chunked upload (media.write scope).",
        # X allows four images in one post — the same "carousel" idea, so the
        # publish tool's item-count check covers it without special-casing.
        supports_carousel=True,
        max_carousel_items=4,
        # Anything over 280 becomes a thread rather than being cut off.
        supports_threads=True,
        max_thread_posts=25,
        # Replies to the account's posts and mentions are readable/answerable —
        # the engagement run reads them and answers through the mode gate.
        supports_comments=True,
        # X is the one platform whose API lets the account LIKE a tweet
        # (POST /2/users/:id/likes). Needs the like.write scope below.
        supports_liking=True,
        # public_metrics on a tweet carries likes/reposts/replies/quotes/
        # impressions — the performance feedback loop reads them per post.
        supports_metrics=True,
        # Recent search (GET /2/tweets/search/recent) finds OTHER people's posts
        # by keyword/#hashtag — the outreach flow's read half.
        supports_search=True,
        # GET /2/dm_events reads inbound DMs, POST /2/dm_conversations/:id/messages
        # answers them — needs the dm.read / dm.write scopes below, so an account
        # connected before they were added must be reconnected.
        supports_dms=True,
    )
    auth_endpoint = "https://x.com/i/oauth2/authorize"
    token_endpoint = f"{API}/oauth2/token"
    scopes = ["tweet.read", "tweet.write", "users.read", "media.write",
              "like.write", "dm.read", "dm.write", "offline.access"]
    # Publishing needs these; the rest power engagement features that an account
    # connected before they existed simply does not have.
    REQUIRED_SCOPES = ("tweet.read", "tweet.write", "users.read", "media.write",
                       "offline.access")
    SCOPE_FEATURES = {"like.write": "liking posts",
                      "dm.read": "reading direct messages",
                      "dm.write": "answering direct messages"}
    use_pkce = True
    token_auth_style = "basic"

    def after_publish(self, *, account, store, result, instruction=None) -> None:
        """Move the community rotation on by one, once the post is live.

        Here rather than inside ``publish`` because it must happen only when the
        post actually landed: advancing on a failed attempt would silently skip a
        community for a whole cycle.

        An instruction that PINNED its destination never used the rotation, so it
        must not advance it either — otherwise a daily pinned instruction would
        walk the cursor past the communities the rotating instructions feed.
        """
        if str(getattr(instruction, "twitter_community_id", "") or "").strip():
            return
        ids = community_ids(account)
        if len(ids) < 2:
            return
        meta = dict(account.meta or {})
        try:
            cursor = int(meta.get("community_cursor", 0))
        except (TypeError, ValueError):
            cursor = 0
        meta["community_cursor"] = (cursor + 1) % len(ids)
        account.set_meta(meta)
        store.upsert_account(account)
        logger.info("X community rotation: posted to %s, next is %s",
                    ids[cursor % len(ids)], ids[meta["community_cursor"]])

    async def fetch_identity(self, access_token: str) -> Identity:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{API}/users/me",
                                 headers={"Authorization": f"Bearer {access_token}"})
            if r.status_code >= 400:
                raise self._api_error(r)
            data = r.json().get("data", {})
        return Identity(external_id=data.get("id", ""), handle=data.get("username", ""))

    async def _upload_media(self, client, access_token, path, media_kind) -> str:
        """Chunked upload via the v2 **sub-path** endpoints, returning the media id.

        X documents two shapes: the legacy ``command=INIT|APPEND|FINALIZE`` form
        parameters on ``POST /2/media/upload``, and dedicated
        ``/initialize``, ``/{id}/append``, ``/{id}/finalize`` endpoints. The
        command form rejected our INIT with a bare 400 ("One or more parameters
        to your request was invalid") — it is the migration-era shell, and
        ``initialize`` wants a **JSON** body with ``total_bytes`` as a real
        integer, not a form field. Use the dedicated endpoints; they are the
        documented contract now.
        """
        data = read_bytes(path)   # blob-aware: falls back to Azure when local is gone
        media_type = _media_type(data, path, media_kind)
        category = "tweet_video" if media_kind == "video" else "tweet_image"
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"{API}/media/upload"

        # INITIALIZE — JSON, and total_bytes must be a number.
        initialize_payload = {"media_type": media_type, "total_bytes": len(data),
                              "media_category": category}
        # A failure before X returns a media id cannot have created upload state
        # or a tweet, so retrying this narrow operation cannot duplicate a post.
        # Do not apply the same rule to FINALIZE or POST /tweets: X may have
        # accepted those even if its response was lost.
        r = None
        for attempt, delay in enumerate((0, 1, 3), start=1):
            if delay:
                await asyncio.sleep(delay)
            r = await client.post(f"{url}/initialize", headers=headers, json=initialize_payload)
            if r.status_code not in _TRANSIENT_UPLOAD_STATUSES:
                break
            request_id = (getattr(r, "headers", {}) or {}).get("x-request-id", "")
            logger.warning("X media initialize returned HTTP %d (attempt %d/3%s); retrying",
                           r.status_code, attempt,
                           f", request id {request_id}" if request_id else "")
        assert r is not None
        if r.status_code >= 400:
            raise self._api_error(r)
        body = r.json()
        media_id = str((body.get("data") or body).get("id") or body.get("media_id") or "")
        if not media_id:
            raise RuntimeError(f"X media initialize returned no id: {str(body)[:300]}")

        # APPEND (chunked) — multipart, one segment per chunk.
        for idx, start in enumerate(range(0, len(data), _CHUNK)):
            chunk = data[start:start + _CHUNK]
            r = await client.post(f"{url}/{media_id}/append", headers=headers,
                                  data={"segment_index": str(idx)},
                                  files={"media": ("chunk", chunk, "application/octet-stream")})
            if r.status_code >= 400:
                raise self._api_error(r)

        # FINALIZE
        r = await client.post(f"{url}/{media_id}/finalize", headers=headers)
        if r.status_code >= 400:
            raise self._api_error(r)
        info = r.json().get("data", r.json())
        processing = info.get("processing_info")

        # STATUS poll (videos)
        while processing and processing.get("state") in {"pending", "in_progress"}:
            await asyncio.sleep(processing.get("check_after_secs", 3))
            r = await client.get(url, headers=headers,
                                 params={"command": "STATUS", "media_id": media_id})
            if r.status_code >= 400:
                raise self._api_error(r)
            processing = r.json().get("data", r.json()).get("processing_info")
        if processing and processing.get("state") == "failed":
            raise RuntimeError(f"X media processing failed: {processing.get('error')}")
        return media_id

    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind,
                      instruction=None, asset_paths=None, placement="feed") -> PublishResult:
        """Post a tweet, with up to four images (X's limit) or one video.

        ``asset_paths`` and ``placement`` are part of the :class:`SocialPlatform`
        contract — ``perform_publish`` always passes them. Omitting them here is
        what made the first ever X publish die with ``got an unexpected keyword
        argument 'asset_paths'`` after the agent had already generated the image.
        """
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        caps = self.capabilities
        paths = [p for p in (asset_paths or [asset_path]) if p]
        # One video, or up to four images — X refuses a mixed set.
        if media_kind == "video":
            paths = paths[:1]
        else:
            paths = paths[: caps.max_carousel_items]

        # The disclosure label is appended to the caption, so on a thread it would
        # land on the LAST post — unseen by anyone who only meets post 1 in their
        # timeline. Pin it to the first instead.
        from .. import disclosure

        posts = split_thread(caption, caps.caption_limit, caps.max_thread_posts,
                             pin_suffix=disclosure.label() if disclosure.enabled(instruction)
                             else "")
        if not posts:
            posts = [""]

        async with httpx.AsyncClient(timeout=120) as client:
            media_ids: list[str] = []
            # No os.path.exists gate: the bytes may live in blob storage.
            if media_kind in {"image", "video"} and paths:
                media_ids = [await self._upload_media(client, access_token, path, media_kind)
                             for path in paths]

            # Chosen ONCE for the whole thread: its later posts belong to the
            # same community as the first.
            community_id = next_community(account, instruction)
            first_id = ""
            reply_to = ""
            for index, text in enumerate(posts):
                payload: dict = {"text": text}
                if community_id:
                    payload["community_id"] = community_id
                    # X's own composer shows this as "Also share with followers".
                    # A community post is otherwise visible only to that
                    # community, so this is the difference between reaching your
                    # audience and reaching a room. Sent ONLY with a community:
                    # the field means nothing on a normal post.
                    if shares_with_followers(account, instruction):
                        payload["share_with_followers"] = True
                # X's own "Made with AI" label — the switch its app shows under
                # Content disclosures. Set on EVERY post of a thread: each post
                # stands alone in a timeline, and the caption suffix is pinned to
                # post 1 precisely because a reader may only ever see one of them.
                payload.update(disclosure.native_flags("twitter", instruction))
                # Media rides on the FIRST post: that is the one shown in a
                # timeline, and X would otherwise repeat the image down the thread.
                if media_ids and index == 0:
                    payload["media"] = {"media_ids": media_ids}
                if reply_to:
                    payload["reply"] = {"in_reply_to_tweet_id": reply_to}
                r = await client.post(f"{API}/tweets", headers=headers, json=payload)
                if r.status_code >= 400:
                    if first_id:
                        # The thread is already partly public; report what exists
                        # rather than losing the id of what did go out.
                        logger.error("Thread broke at post %d/%d: %s",
                                     index + 1, len(posts), self._api_error(r))
                        break
                    raise self._api_error(r)
                data = r.json().get("data", {})
                reply_to = data.get("id", "")
                if index == 0:
                    first_id, first_data = reply_to, data

        if len(posts) > 1:
            logger.info("Posted an X thread of %d posts (%d chars)", len(posts), len(caption or ""))
        handle = account.handle or "i"
        return PublishResult(
            url=f"https://x.com/{handle}/status/{first_id}" if first_id else "",
            external_id=first_id,
            raw={**first_data, "thread_posts": len(posts)} if first_id else {})


    # ------------------------------------------------------------------ #
    # Reading and engagement
    #
    # NOTE ON BILLING: since February 2026 the X API is PAY-PER-USE with no free
    # tier — you buy credits and every call, read or write, spends them. An
    # account out of credits gets 402 on everything, including posting. The tools
    # surface that verbatim rather than pretending the account is empty, because
    # "no posts", "not allowed" and "out of credits" need very different
    # responses from the agent.
    # ------------------------------------------------------------------ #
    TWEET_FIELDS = "id,text,created_at,public_metrics,conversation_id,referenced_tweets"

    @staticmethod
    def _api_error(response: httpx.Response) -> RuntimeError:
        """X errors are JSON; surface the reason instead of a bare status code.

        402 gets spelled out because httpx's own message ("Client error '402
        Payment Required'") tells you nothing actionable, and the cause is not a
        bug in the post: X moved to **pay-per-use credits** in February 2026, so
        an account with no credits cannot publish at all.

        ``detail`` and ``errors[]`` are BOTH reported. On a 400 the top-level
        detail is the useless generic "One or more parameters to your request
        was invalid" while ``errors[].message`` names the actual parameter —
        reading detail alone turned a precise complaint into a guessing game
        during the media-upload migration.
        """
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = {}
        specifics = [str(e.get("message") or e.get("detail") or "").strip()
                     for e in (body.get("errors") or []) if isinstance(e, dict)]
        specifics = [s for s in specifics if s]
        detail = body.get("detail") or body.get("title") or (response.text or "")[:300]
        for extra in specifics:
            if extra not in detail:
                detail = f"{detail} ({extra})" if detail else extra
        hint = ""
        if response.status_code == 402:
            hint = (" — this is BILLING, not a problem with the post. The X API is "
                    "pay-per-use and your developer account has no credits left. "
                    "Buy credits at https://console.x.com and retry; nothing in the "
                    "post or the account connection needs changing.")
        elif response.status_code == 403 and _is_reply_permission_block(detail):
            # X refuses outbound replies to strangers on some access tiers /
            # accounts: "replies are only allowed to posts where the account is
            # mentioned or is the author". This is NOT a token/scope problem —
            # reconnecting fixes nothing — so say so, or an outreach operator
            # burns a reconnect chasing the wrong cause. Likes/search still work.
            hint = (" — X is refusing OUTBOUND replies for this account: it only lets "
                    "you reply to posts that mention you or that you authored. This is an "
                    "X-side reply restriction on outreach, NOT a token or app-permission "
                    "problem — reconnecting will not change it. Likes and search still work, "
                    "so use those for outreach, or reply only where the account is mentioned.")
        elif response.status_code in (401, 403):
            hint = (" — the token is rejected or the app lacks access for this call. "
                    "Check the app's permissions and that the account is still "
                    "connected; reads and writes both consume API credits.")
        elif response.status_code == 429:
            hint = " — rate limited; wait before retrying."
        elif response.status_code >= 500:
            # X's 5xx are frequent, and have run for hours at a time on a single
            # endpoint (media upload while tweets worked, and the reverse). None
            # of it is caused by the caption, the media, the token or the app,
            # and rewriting the post is wasted effort.
            hint = (" — this is X's own service failing, NOT your post, token, app or "
                    "billing. X returns 5xx on one endpoint at a time and it can last "
                    "hours. Do not regenerate the content: when X recovers, use "
                    "'Publish this again' on the failed run to send the same media. "
                    "Check which endpoint is affected with: python scripts/diagnose_x.py")
        # X support can trace a failed request by this id. It is safe to show:
        # unlike Authorization, it grants no access and expires with X's logs.
        headers = getattr(response, "headers", {}) or {}
        request_id = (headers.get("x-request-id") or headers.get("x-transaction-id")
                      or headers.get("x-client-transaction-id") or "")
        trace = f" [X request id: {request_id}]" if request_id else ""
        return RuntimeError(f"X API {response.status_code}: {detail}{trace}{hint}")

    async def _get(self, access_token: str, path: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{API}/{path}", params=params,
                                 headers={"Authorization": f"Bearer {access_token}"})
            if r.status_code >= 400:
                raise self._api_error(r)
            return r.json()

    async def list_posts(self, access_token: str, account: Account, *,
                         limit: int = 10) -> list[dict]:
        """This account's own recent posts — so the agent doesn't repeat itself."""
        payload = await self._get(access_token, f"users/{account.external_id}/tweets", {
            "max_results": max(5, min(limit, 100)), "tweet.fields": self.TWEET_FIELDS})
        return payload.get("data", []) or []

    async def list_communities(self, access_token: str, account: Account) -> list[dict]:
        """Communities this X user has joined, suitable as post targets."""
        payload = await self._get(access_token, f"users/{account.external_id}/communities", {
            "max_results": 100, "community.fields": "id,name,description,member_count"})
        return [{"id": str(c.get("id", "")), "name": c.get("name", ""),
                 "description": c.get("description", "")}
                for c in payload.get("data", []) or [] if c.get("id")]

    async def community_name(self, access_token: str, community_id: str) -> str:
        """One community's NAME, from ``GET /2/communities/:id``.

        A 19-digit id is not something anyone recognises, and an operator picking
        a destination should see "AI Builders", not 1493446837214187523. Used to
        label ids the account listing did not cover — a community the operator
        pasted from a URL without having joined it, say.

        Best effort by design: X is pay-per-use and this endpoint is not
        available to every app tier, so a failure means "we could not learn the
        name", never "this id is wrong". The caller falls back to the id and the
        operator can type a label instead.
        """
        cid = str(community_id or "").strip()
        if not cid:
            return ""
        try:
            payload = await self._get(access_token, f"communities/{cid}",
                                      {"community.fields": "id,name"})
        except Exception as exc:  # noqa: BLE001
            logger.info("Could not look up X community %s: %s", cid, exc)
            return ""
        return str((payload.get("data") or {}).get("name") or "")

    async def resolve_community_names(self, access_token: str, account: Account,
                                      ids: list[str]) -> dict:
        """``{id: name}`` for the given ids, as far as X will tell us.

        Tries the account's joined-communities listing FIRST — one call for all of
        them — and only then looks up individually whatever it did not cover.
        """
        names: dict[str, str] = {}
        wanted = [str(i).strip() for i in (ids or []) if str(i).strip()]
        if not wanted:
            return names
        try:
            for community in await self.list_communities(access_token, account):
                if community["id"] in wanted and community["name"]:
                    names[community["id"]] = community["name"]
        except Exception as exc:  # noqa: BLE001 - the per-id lookup still runs
            logger.info("Could not list X communities for %s: %s", account.handle, exc)
        for cid in wanted:
            if cid not in names:
                name = await self.community_name(access_token, cid)
                if name:
                    names[cid] = name
        return names

    async def list_mentions(self, access_token: str, account: Account, *,
                            limit: int = 10) -> list[dict]:
        """Posts that mentioned this account — the other half of engagement."""
        payload = await self._get(access_token, f"users/{account.external_id}/mentions", {
            "max_results": max(5, min(limit, 100)), "tweet.fields": self.TWEET_FIELDS})
        return payload.get("data", []) or []

    async def post_metrics(self, access_token: str, post_id: str) -> dict:
        """Impressions/likes/reposts for one post."""
        payload = await self._get(access_token, f"tweets/{post_id}",
                                  {"tweet.fields": self.TWEET_FIELDS})
        return payload.get("data", {}) or {}

    async def fetch_post_metrics(self, access_token: str, account: Account, *,
                                 external_id: str) -> dict | None:
        """Normalized per-post counters for the performance feedback loop.

        ``public_metrics`` on a tweet carries the counters anyone can see; map them
        onto the shared metric names. Returns ``None`` on any failure so a single
        unreadable post never breaks the sweep — the caller just leaves last
        known metrics in place.
        """
        if not external_id:
            return None
        try:
            data = await self.post_metrics(access_token, external_id)
        except Exception as exc:  # noqa: BLE001 - one bad post must not stop the sweep
            logger.warning("X metrics for %s failed: %s", external_id, exc)
            return None
        pm = data.get("public_metrics") or {}
        return {
            "likes": pm.get("like_count", 0),
            "reposts": pm.get("retweet_count", 0),
            "replies": pm.get("reply_count", 0),
            "quotes": pm.get("quote_count", 0),
            "impressions": pm.get("impression_count", 0),
        }

    # GET /2/tweets takes up to 100 ids in one request. The daily sweep asks
    # about every post published inside METRICS_REFRESH_DAYS, so on this
    # pay-per-use API that is the difference between one request a day and one
    # per post per day, growing with the account's history.
    MAX_LOOKUP_IDS = 100

    async def fetch_post_metrics_bulk(self, access_token: str, account: Account, *,
                                      external_ids: list[str]) -> dict[str, dict]:
        """Counters for many posts in ONE ``GET /2/tweets?ids=…`` per 100.

        Best-effort per CHUNK: a failed chunk is logged and skipped so one bad id
        (a deleted post makes the whole lookup partial, not fatal) never stops the
        sweep. X reports unreadable ids under ``errors`` and simply omits them
        from ``data``, which is exactly the "absent means not readable" contract
        the base method documents.
        """
        wanted = [str(i) for i in external_ids if i]
        out: dict[str, dict] = {}
        for start in range(0, len(wanted), self.MAX_LOOKUP_IDS):
            chunk = wanted[start:start + self.MAX_LOOKUP_IDS]
            try:
                payload = await self._get(access_token, "tweets", {
                    "ids": ",".join(chunk), "tweet.fields": self.TWEET_FIELDS})
            except Exception as exc:  # noqa: BLE001 - one chunk must not stop the sweep
                logger.warning("X bulk metrics for %d post(s) failed: %s", len(chunk), exc)
                continue
            for data in payload.get("data", []) or []:
                post_id = str(data.get("id") or "")
                if not post_id:
                    continue
                pm = data.get("public_metrics") or {}
                out[post_id] = {
                    "likes": pm.get("like_count", 0),
                    "reposts": pm.get("retweet_count", 0),
                    "replies": pm.get("reply_count", 0),
                    "quotes": pm.get("quote_count", 0),
                    "impressions": pm.get("impression_count", 0),
                }
        return out

    async def profile(self, access_token: str) -> dict:
        """Follower and post counts for the connected account."""
        payload = await self._get(access_token, "users/me", {
            "user.fields": "id,name,username,description,public_metrics,verified"})
        return payload.get("data", {}) or {}

    async def list_replies(self, access_token: str, account: Account, *,
                           limit: int = 10, since_posts: int = 5) -> list[dict]:
        """Replies OTHERS wrote under this account's recent posts.

        Mentions cover "someone @-ed us"; this covers "someone replied to what we
        posted" — the comment thread under a tweet. X has no direct "get replies"
        endpoint, so we take the account's recent posts, then run one recent
        search for tweets whose ``conversation_id`` is one of those posts and that
        are replies not written by the account itself.

        Kept deliberately small (recent search + posts both spend credits on the
        pay-per-use API) and best-effort: recent search needs project access some
        apps lack, so a failure returns ``[]`` rather than killing the run — the
        agent still has mentions to work from.
        """
        posts = await self.list_posts(access_token, account, limit=max(1, min(since_posts, 10)))
        conversation_ids = [str(p.get("id")) for p in posts if p.get("id")]
        if not conversation_ids:
            return []
        convo = " OR ".join(f"conversation_id:{cid}" for cid in conversation_ids)
        handle = (account.handle or "").lstrip("@")
        query = f"({convo}) is:reply" + (f" -from:{handle}" if handle else "")
        try:
            payload = await self._get(access_token, "tweets/search/recent", {
                "query": query, "max_results": max(10, min(limit, 100)),
                "tweet.fields": f"{self.TWEET_FIELDS},author_id",
                "expansions": "author_id", "user.fields": "username"})
        except Exception as exc:  # noqa: BLE001 - reads are best-effort here
            logger.warning("X reply search failed (%s); returning no replies", exc)
            return []

        # Drop the account's OWN replies by author id, and de-duplicate by tweet
        # id. The ``-from:{handle}`` clause above is the first line of defence,
        # but it is a fragile string: an empty/renamed handle, or a case the
        # search operator does not match, lets the account's own replies through
        # — and the agent then answers its OWN reply, posting a second reply
        # under a comment it already handled. Keying on the numeric author id
        # (always this account's ``external_id``) can't miss that way, so a
        # ``-from:`` slip no longer turns the thread into a self-reply loop.
        me = str(account.external_id or "")
        seen: set[str] = set()
        replies: list[dict] = []
        for p in payload.get("data", []) or []:
            pid = str(p.get("id") or "")
            if not pid or pid in seen:
                continue
            if me and str(p.get("author_id") or "") == me:
                continue
            seen.add(pid)
            replies.append(p)
        return replies

    async def search_content(self, access_token: str, account: Account, *,
                             query: str, limit: int = 10, subreddit: str = "") -> list[dict]:
        """Recent posts from OTHER accounts matching ``query`` — the outreach read.

        Runs one ``GET /2/tweets/search/recent`` for the agent to find strangers'
        posts to reply to or like. The query is narrowed to ORIGINAL posts worth
        engaging: ``-is:retweet -is:reply`` (a retweet is not the author's words
        and a reply is buried in a thread), and ``-from:{handle}`` so the account
        never surfaces its own posts. ``subreddit`` is ignored (Reddit-only).

        Best-effort and small — recent search spends credits on the pay-per-use
        API and needs project access some apps lack, so a failure returns ``[]``
        rather than killing the run. Returns normalized items with ``author`` (the
        poster's ``@handle``) so the agent can see it is engaging someone else, and
        a ``repliable`` flag from each post's ``reply_settings`` — replying to a
        restricted post the account is not mentioned in is the documented 403, so
        the agent must only reply where ``repliable`` is true (likes still work on
        any of them).
        """
        q = (query or "").strip()
        if not q:
            return []
        me = (account.handle or "").lstrip("@")
        full = f"({q}) -is:retweet -is:reply" + (f" -from:{me}" if me else "")
        try:
            payload = await self._get(access_token, "tweets/search/recent", {
                "query": full, "max_results": max(10, min(limit, 100)),
                "tweet.fields": f"{self.TWEET_FIELDS},reply_settings,author_id",
                "expansions": "author_id", "user.fields": "username"})
        except Exception as exc:  # noqa: BLE001 - outreach search is best-effort
            logger.warning("X content search failed (%s); returning no results", exc)
            return []
        users = {u.get("id"): u.get("username")
                 for u in (payload.get("includes", {}) or {}).get("users", []) or []}
        my_id = str(account.external_id or "")
        items: list[dict] = []
        for p in payload.get("data", []) or []:
            author_id = str(p.get("author_id") or "")
            if my_id and author_id == my_id:      # never engage our own post
                continue
            username = users.get(p.get("author_id"), "")
            metrics = p.get("public_metrics", {}) or {}
            reply_settings = p.get("reply_settings")
            items.append({
                "id": str(p.get("id") or ""),
                "text": (p.get("text") or "")[:600],
                "url": f"https://x.com/{username or 'i'}/status/{p.get('id')}",
                "author": f"@{username}" if username else "",
                "posted_at": p.get("created_at"),
                "likes": metrics.get("like_count"),
                "reposts": metrics.get("retweet_count"),
                "replies": metrics.get("reply_count"),
                "reply_settings": reply_settings or "everyone",
                "repliable": _is_repliable(reply_settings),
            })
        return items[:limit]

    # ------------------------------------------------------------------ #
    # Direct messages
    # ------------------------------------------------------------------ #
    DM_FIELDS = "id,text,created_at,sender_id,dm_conversation_id,event_type"

    async def list_dms(self, access_token: str, account: Account, *,
                       limit: int = 25) -> list[dict]:
        """Recent INBOUND direct messages awaiting a reply.

        ``GET /2/dm_events`` returns the account's most recent DM events across all
        conversations, newest first. We keep only ``MessageCreate`` events (a join/
        leave is not a message) NOT sent by this account (``sender_id`` !=
        ``external_id`` — the same self-exclusion the reply search uses), so the
        agent never answers its own outbound message. ``id`` is the inbound message
        (event) id the ledger dedupes on; ``conversation_id`` is where a reply is
        sent. Needs the ``dm.read`` scope, which an old connection may not have —
        and `_api_error` explains a 401/403 far better than an empty list does.
        A failure RAISES rather than returning ``[]``: the tool layer turns it into a
        message the agent can act on, and "cannot read DMs" must not look like "no
        DMs" — that is how a broken Instagram DM read went unnoticed for weeks.
        """
        payload = await self._get(access_token, "dm_events", {
            "max_results": max(1, min(limit, 100)),
            "dm_event.fields": self.DM_FIELDS,
            "expansions": "sender_id", "user.fields": "username"})
        users = {u.get("id"): u.get("username")
                 for u in (payload.get("includes", {}) or {}).get("users", []) or []}
        me = str(account.external_id or "")
        items: list[dict] = []
        for ev in payload.get("data", []) or []:
            if ev.get("event_type") != "MessageCreate":
                continue
            sender_id = str(ev.get("sender_id") or "")
            if me and sender_id == me:            # skip our own outbound messages
                continue
            username = users.get(ev.get("sender_id"), "")
            items.append({
                "id": str(ev.get("id") or ""),
                "conversation_id": str(ev.get("dm_conversation_id") or ""),
                "sender": f"@{username}" if username else "",
                "sender_id": sender_id,
                "text": (ev.get("text") or "")[:1000],
                "created_at": ev.get("created_at"),
            })
        return items[:limit]

    async def send_dm(self, access_token: str, conversation_id: str, text: str) -> dict:
        """Send a message into an existing DM conversation. Sends IMMEDIATELY.

        ``POST /2/dm_conversations/{id}/messages`` — the conversation id comes from
        the inbound message's ``dm_conversation_id`` (:meth:`list_dms`), so we reply
        into the same thread. Needs the ``dm.write`` scope.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{API}/dm_conversations/{conversation_id}/messages",
                headers={"Authorization": f"Bearer {access_token}",
                         "Content-Type": "application/json"},
                json={"text": text})
            if r.status_code >= 400:
                raise self._api_error(r)
            return r.json().get("data", {}) or {}

    async def reply(self, access_token: str, post_id: str, text: str) -> dict:
        """Reply to a post, in the account's voice. Posts IMMEDIATELY."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{API}/tweets",
                headers={"Authorization": f"Bearer {access_token}",
                         "Content-Type": "application/json"},
                json={"text": text[: self.capabilities.caption_limit],
                      "reply": {"in_reply_to_tweet_id": post_id}})
            if r.status_code >= 400:
                raise self._api_error(r)
            return r.json().get("data", {})

    async def reply_to_target(self, access_token: str, account: Account, *,
                              target_type: str, target_id: str, text: str,
                              reply_to: str = "") -> dict:
        """Reply to a post/mention/reply, or answer a DM (the mode-gated path).

        A ``dm`` target is sent into its conversation: ``reply_to`` carries the
        ``dm_conversation_id`` from :meth:`list_dms` (``target_id`` is the inbound
        message id the ledger dedupes on, which is NOT a send destination), and DMs
        have no public permalink so ``url`` is empty. Every other X target is a
        tweet id, so this delegates to :meth:`reply` and builds the reply's public
        url from the account handle and the new id.
        """
        if (target_type or "").lower() == "dm":
            if not reply_to:
                raise RuntimeError("X needs the DM conversation id to reply — none was given.")
            data = await self.send_dm(access_token, reply_to, text)
            return {"id": str(data.get("dm_event_id") or ""), "url": ""}
        data = await self.reply(access_token, target_id, text)
        new_id = data.get("id", "")
        handle = (account.handle or "i").lstrip("@")
        return {"id": new_id,
                "url": f"https://x.com/{handle}/status/{new_id}" if new_id else ""}

    async def like_target(self, access_token: str, account: Account, *,
                          target_type: str, target_id: str, like: bool = True) -> dict:
        """Like or un-like a tweet on behalf of the connected account.

        ``POST /2/users/{id}/likes`` (body ``{"tweet_id": ...}``) to like,
        ``DELETE /2/users/{id}/likes/{tweet_id}`` to un-like — both need the
        ``like.write`` scope, so an account connected before it was added must be
        reconnected. Liking is idempotent (X returns ``liked: true`` for a tweet
        already liked), so the engagement flow never needs a "already liked"
        ledger the way replies do.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            if like:
                r = await client.post(
                    f"{API}/users/{account.external_id}/likes",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"tweet_id": target_id})
            else:
                r = await client.delete(
                    f"{API}/users/{account.external_id}/likes/{target_id}", headers=headers)
            if r.status_code >= 400:
                raise self._api_error(r)
            data = r.json().get("data", {}) or {}
        return {"liked": data.get("liked", like), "id": target_id}

    async def delete_post(self, access_token: str, post_id: str) -> dict:
        """Delete one of this account's own posts."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.delete(f"{API}/tweets/{post_id}",
                                    headers={"Authorization": f"Bearer {access_token}"})
            if r.status_code >= 400:
                raise self._api_error(r)
            return r.json().get("data", {})

    async def post_exists(self, access_token: str, account: Account,
                          external_id: str) -> bool | None:
        """Is this post still up? Used by the duplicate guard before it refuses.

        ``None`` when it cannot tell — including a 402/403, where "cannot check"
        must not be mistaken for "was deleted".
        """
        if not external_id:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(f"{API}/tweets/{external_id}", params={"ids": external_id},
                                     headers={"Authorization": f"Bearer {access_token}"})
            if r.status_code == 404:
                return False
            if r.status_code >= 400:
                return None
            body = r.json()
            if body.get("data"):
                return True
            errors = body.get("errors") or []
            # X reports a deleted post as a 200 with an errors[] entry.
            if any((e.get("title") or "").lower().startswith("not found") for e in errors):
                return False
            return None
        except Exception as exc:  # noqa: BLE001 - diagnostics never break a publish
            logger.warning("X post lookup failed for %s: %s", external_id, exc)
            return None


register(PlatformName.twitter, Twitter)
