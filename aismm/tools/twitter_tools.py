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
            posts = await platform.list_posts(token, account, limit=limit)
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
            posts = await platform.list_mentions(token, account, limit=limit)
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
            posts = await platform.list_replies(token, account, limit=limit)
            return {"count": len(posts), "replies": [_post_view(p, account) for p in posts]}

        return await _with_context(state, call)

    return x_replies


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
register_tool("x_reply_to_post", _make_reply)
register_tool("x_like_post", _make_like)
register_tool("x_post_metrics", _make_post_metrics)
register_tool("x_profile", _make_profile)
register_tool("x_delete_post", _make_delete_post)
