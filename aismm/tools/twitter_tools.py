"""X (Twitter)-specific tools: read the account, engage, check how a post did.

The Instagram counterpart of this module turned the agent from a publisher into
an account manager; this does the same for X. Publishing still goes through the
one gated ``publish`` tool — everything *else* X offers lives here:

* ``x_recent_posts``  — what this account has already posted, so the agent does
  not repeat itself and can pick up its own established voice.
* ``x_mentions`` / ``x_reply_to_post`` — see who is talking to the account and
  answer them.
* ``x_post_metrics`` / ``x_profile`` — how a post did, and follower counts, so a
  brief that says "lean into what works" has something to work from.
* ``x_delete_post`` — remove one of the account's own posts.

**X billing matters here.** Since February 2026 the X API is pay-per-use with no
free tier: you buy credits up front and every call — read or write — spends them.
An account with no credits gets **402 Payment Required on everything, including
posting**. The platform layer spells that out in the error rather than returning
an empty list, because "this account has no posts" and "your account is out of
credits" call for completely different behaviour from the agent.

Every factory returns ``None`` unless the run targets an X account, so an
Instagram run is not handed six irrelevant tools. As with Instagram, the write
tools (reply, delete) act on the real account IMMEDIATELY and are **not** behind
``publish_mode`` — that gate is about posting content, and an approval queue for
replies would make them useless.
"""
from __future__ import annotations

import logging

from agents import function_tool

from .. import engagement, engagement_ledger, tokens
from ..models import PlatformName
from .registry import register_tool

logger = logging.getLogger("aismm.tools.twitter")

# One canonical target kind for every X tweet the account might answer — a
# mention and a reply are both just tweet ids, so keying the ledger on "tweet"
# (not "mention" vs "reply") means the read tools' already_answered flag and the
# reply tool's recorded fingerprint always line up, and dedup actually works.
_X_TARGET = "tweet"
# DMs are a separate target kind: the ledger keys on the inbound MESSAGE id, and
# a "dm" id never collides with a "tweet" id even if the numbers coincide.
_X_DM = "dm"


def _guard(state: dict) -> bool:
    """Factory helper: only build these tools for an X run."""
    account = state.get("account")
    return account is not None and account.platform is PlatformName.twitter


async def _context(state: dict):
    account = state.get("account")
    if account is None or account.platform is not PlatformName.twitter:
        return None
    from ..platforms.registry import get_platform  # lazy

    token = await tokens.valid_access_token(account, state["store"])
    if not token:
        return None
    return get_platform(PlatformName.twitter), account, token


async def _with_context(state: dict, call):
    context = await _context(state)
    if context is None:
        return {"error": "not_available",
                "message": "This run does not target a connected X account."}
    platform, account, token = context
    try:
        return await call(platform, account, token)
    except Exception as exc:  # noqa: BLE001 - report, never kill the run
        logger.warning("X tool failed: %s", exc)
        return {"error": "x_api_error", "message": str(exc)}


# X is pay-per-use and a READ is charged twice over — once as a request, and again
# for every post object it returns. A model that reads its replies, answers two of
# them and then reads them again "to check" pays the full price for the second look
# at a list that has not moved: an engagement read is a snapshot of what OTHER
# people wrote, and nobody wrote anything new in the ninety seconds since.
#
# So the raw platform items are cached for the life of the run and only the VIEW is
# rebuilt on each call — `already_answered` is recomputed from the ledger every
# time, so a cached list still shows the agent exactly what it has just handled.
# The cache is per-run state, so the next scheduled fire reads X afresh.
_READ_CACHE = "_x_reads"


async def _cached_read(state: dict, key: tuple, fetch):
    """Fetch once per run for a given (tool, arguments) pair.

    A failed read is deliberately NOT cached: an error is not an answer, and the
    agent retrying after a rate limit must be able to reach X again.
    """
    cache = state.setdefault(_READ_CACHE, {})
    if key not in cache:
        cache[key] = await fetch()
    return cache[key]


def _post_view(post: dict, account=None) -> dict:
    metrics = post.get("public_metrics", {}) or {}
    view = {
        "id": post.get("id"),
        "text": (post.get("text") or "")[:600],
        "posted_at": post.get("created_at"),
        "likes": metrics.get("like_count"),
        "reposts": metrics.get("retweet_count"),
        "replies": metrics.get("reply_count"),
        "quotes": metrics.get("quote_count"),
        "impressions": metrics.get("impression_count"),
    }
    # For an engagement read, flag what this account already answered so the agent
    # skips it rather than replying to the same tweet on every scheduled run.
    if account is not None:
        view["already_answered"] = engagement_ledger.answered(account, _X_TARGET, post.get("id"))
    return view


def _make_recent_posts(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_recent_posts(limit: int = 10) -> dict:
        """List this account's recent posts on X, with their engagement counts.

        Read this before choosing a topic: it is the fastest way to avoid
        repeating a post, and it shows the account's established voice in its own
        words.

        Args:
            limit: How many posts to return (5–100, newest first).
        """
        async def call(platform, account, token):
            posts = await _cached_read(
                state, ("posts", limit),
                lambda: platform.list_posts(token, account, limit=limit))
            return {"count": len(posts), "posts": [_post_view(p) for p in posts]}

        return await _with_context(state, call)

    return x_recent_posts


def _make_mentions(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_mentions(limit: int = 10) -> dict:
        """Posts that mentioned this account — who is talking to it, and about what.

        Use it when the brief asks you to engage, then answer with
        ``x_reply_to_post``.

        Args:
            limit: How many mentions to return (5–100, newest first).
        """
        async def call(platform, account, token):
            posts = await _cached_read(
                state, ("mentions", limit),
                lambda: platform.list_mentions(token, account, limit=limit))
            return {"count": len(posts), "mentions": [_post_view(p, account) for p in posts]}

        return await _with_context(state, call)

    return x_mentions


def _make_replies(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_replies(limit: int = 10) -> dict:
        """Replies OTHERS left under this account's recent posts — the comment
        thread on X, as opposed to ``x_mentions`` (someone @-ing the account).

        Use it on an engagement run to find comments to answer, then reply with
        ``x_reply_to_post``. Items flagged ``already_answered`` have been handled —
        skip them. Best-effort: X's reply search needs project access some apps
        lack, so an empty list can mean "none" or "not available on this app".

        Args:
            limit: How many replies to return (newest-ish first).
        """
        async def call(platform, account, token):
            # The costliest read on X — a timeline lookup AND a recent search, so
            # a repeat call is two requests and ~15 post objects for nothing.
            posts = await _cached_read(
                state, ("replies", limit),
                lambda: platform.list_replies(token, account, limit=limit))
            return {"count": len(posts), "replies": [_post_view(p, account) for p in posts]}

        return await _with_context(state, call)

    return x_replies


def _make_search(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_search_posts(query: str = "", limit: int = 10) -> dict:
        """Find OTHER people's recent posts to engage — the OUTREACH search.

        Searches X's recent posts for ORIGINAL tweets (no retweets, no replies)
        matching your query, so you can reply to or like strangers' posts and grow
        the account's reach. Leave ``query`` empty to search the instruction's
        configured outreach targets (its keywords + #hashtags); pass one to search
        something specific you inferred from the brief. Items flagged
        ``already_answered`` have already been engaged by this account — skip them.

        Each item also carries ``repliable``: only reply (``x_reply_to_post``) to
        posts where it is ``true``. A ``false`` post has the author's reply setting
        restricted (followers / mentioned users only), so X will REFUSE your reply
        with a 403 — do not attempt it. You may still ``x_like_post`` any post,
        repliable or not.

        Args:
            query: X recent-search query. Empty = use the instruction's targets.
            limit: How many posts to return (10–100, newest first).
        """
        q = (query or "").strip()
        if not q:
            from ..targets import x_query

            instruction = state.get("instruction")
            targets = instruction.parsed_targets if instruction is not None else None
            q = x_query(targets) if targets else ""
        if not q:
            return {"error": "no_query",
                    "message": ("No query given and the instruction has no outreach "
                                "targets set. Infer a keyword or #hashtag from the brief "
                                "and pass it as `query`.")}

        async def call(platform, account, token):
            posts = await _cached_read(
                state, ("search", q, limit),
                lambda: platform.search_content(token, account, query=q, limit=limit))
            for p in posts:
                p["already_answered"] = engagement_ledger.answered(
                    account, _X_TARGET, p.get("id"))
            return {"query": q, "count": len(posts), "posts": posts}

        return await _with_context(state, call)

    return x_search_posts


def _make_reply(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_reply_to_post(post_id: str, text: str, replying_to: str = "") -> dict:
        """Reply publicly to a post, in the account's voice.

        The reply goes out through the instruction's publish mode, exactly like a
        post: ``dry_run`` stages a preview, ``approval`` queues it for a human, and
        only ``live`` sends it now. A result of "staged"/"pending_approval" means
        it did its job. Be brief and helpful, never argue, and never promise
        anything on the account's behalf. A post already answered (or already
        queued) comes back "skipped" — move on.

        Args:
            post_id: The id of the post to reply to (from ``x_mentions`` / ``x_replies``).
            text: The reply, 280 characters max.
            replying_to: The text of the post you are answering, so a human
                reviewing the queue can see what the reply responds to.
        """
        if not _guard(state):
            return {"error": "not_available",
                    "message": "This run does not target a connected X account."}
        return await engagement.perform_reply(
            state, target_type=_X_TARGET, target_id=post_id, text=text,
            target_excerpt=replying_to)

    return x_reply_to_post


def _make_dms(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_dms(limit: int = 25) -> dict:
        """Recent INBOUND direct messages to this account — private messages to answer.

        The private counterpart of ``x_replies``: read new DMs and answer the ones
        that need it with ``x_reply_to_dm``. Each item carries the ``id`` (the
        message id — pass it back as ``message_id``), ``conversation_id`` (pass it
        back as ``conversation_id`` so the reply lands in the same thread),
        ``sender`` and ``text``. Items flagged ``already_answered`` have been
        handled — skip them. Needs the ``dm.read`` scope; an account connected
        before it was added must be reconnected. Best-effort: an empty list can
        mean "no new DMs" or "the scope is missing".

        Args:
            limit: How many messages to return (1–100, newest first).
        """
        async def call(platform, account, token):
            dms = await _cached_read(
                state, ("dms", limit),
                lambda: platform.list_dms(token, account, limit=limit))
            for d in dms:
                d["already_answered"] = engagement_ledger.answered(
                    account, _X_DM, d.get("id"))
            engagement.note_read(
                state, "x_dms",
                unanswered=sum(1 for d in dms if not d.get("already_answered")))
            logger.info("DMs read for %s: %d inbound message(s), %d unanswered",
                        account.handle or account.external_id, len(dms),
                        sum(1 for d in dms if not d.get("already_answered")))
            return {"count": len(dms), "dms": dms}

        return await _with_context(state, call)

    return x_dms


def _make_reply_to_dm(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_reply_to_dm(message_id: str, conversation_id: str, text: str,
                            replying_to: str = "") -> dict:
        """Answer a direct message, in the account's voice.

        The reply goes out through the instruction's publish mode, exactly like a
        public reply: ``dry_run`` stages a preview, ``approval`` queues it for a
        human, only ``live`` sends it now. A DM already answered (or already queued)
        comes back "skipped" — move on. Be brief and helpful, never promise anything
        on the account's behalf, and never send unsolicited DMs — only answer what
        arrived.

        Args:
            message_id: The inbound message's ``id`` (from ``x_dms``) — what is
                deduped so the same DM is never answered twice.
            conversation_id: The message's ``conversation_id`` (from ``x_dms``) —
                where the reply is delivered.
            text: The reply to send.
            replying_to: The text of the DM you are answering, shown to a human
                reviewing the queue.
        """
        if not _guard(state):
            return {"error": "not_available",
                    "message": "This run does not target a connected X account."}
        return await engagement.perform_reply(
            state, target_type=_X_DM, target_id=message_id, text=text,
            reply_to=conversation_id, target_excerpt=replying_to)

    return x_reply_to_dm


def _make_like(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_like_post(post_id: str, like: bool = True) -> dict:
        """Like a post on X — a mention or a reply under this account's posts.

        A like is the right, low-key response when a comment is warm, supportive,
        or a simple "thanks" that needs acknowledging but not a written reply. Use
        it freely alongside ``x_reply_to_post``: like the ones you are answering
        AND the friendly ones you are not. Unlike a reply, a like is NOT gated by
        the publish mode — it sends immediately. It is idempotent, so liking a
        post you already liked is harmless.

        Args:
            post_id: The id of the post to like (from ``x_mentions`` / ``x_replies``).
            like: ``True`` to like (default), ``False`` to remove a previous like.
        """
        async def call(platform, account, token):
            result = await platform.like_target(
                token, account, target_type=_X_TARGET, target_id=post_id, like=like)
            logger.info("%s X post %s", "Liked" if like else "Un-liked", post_id)
            return {"status": "liked" if result.get("liked") else "unliked", **result}

        return await _with_context(state, call)

    return x_like_post


def _make_post_metrics(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_post_metrics(post_id: str) -> dict:
        """How one post performed — impressions, likes, reposts, replies.

        Args:
            post_id: A post id from ``x_recent_posts``.
        """
        async def call(platform, account, token):
            return _post_view(await platform.post_metrics(token, post_id))

        return await _with_context(state, call)

    return x_post_metrics


def _make_profile(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_profile() -> dict:
        """This account's bio, follower count and post count on X."""
        async def call(platform, account, token):
            data = await platform.profile(token)
            metrics = data.get("public_metrics", {}) or {}
            return {"handle": data.get("username"), "name": data.get("name"),
                    "bio": data.get("description"),
                    "followers": metrics.get("followers_count"),
                    "following": metrics.get("following_count"),
                    "posts": metrics.get("tweet_count")}

        return await _with_context(state, call)

    return x_profile


def _make_delete_post(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def x_delete_post(post_id: str) -> dict:
        """Delete one of THIS account's own posts. Immediate and irreversible.

        Only for correcting something this account published — a factual error, a
        duplicate. Never use it on anyone else's post (X would refuse anyway).

        Args:
            post_id: The id of the post to delete.
        """
        async def call(platform, account, token):
            result = await platform.delete_post(token, post_id)
            logger.warning("Deleted X post %s", post_id)
            return {"status": "deleted" if result.get("deleted") else "unknown",
                    "id": post_id}

        return await _with_context(state, call)

    return x_delete_post


register_tool("x_recent_posts", _make_recent_posts)
register_tool("x_mentions", _make_mentions)
register_tool("x_replies", _make_replies)
register_tool("x_search_posts", _make_search)
register_tool("x_reply_to_post", _make_reply)
register_tool("x_dms", _make_dms)
register_tool("x_reply_to_dm", _make_reply_to_dm)
register_tool("x_like_post", _make_like)
register_tool("x_post_metrics", _make_post_metrics)
register_tool("x_profile", _make_profile)
register_tool("x_delete_post", _make_delete_post)
