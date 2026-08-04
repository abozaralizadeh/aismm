"""Instagram-specific tools: read the account, engage, check the quota.

Publishing goes through the one gated ``publish`` tool (carousels and stories
included — see ``publish``'s ``asset_paths`` / ``placement``). Everything *else*
Instagram offers lives here, because an account manager does more than post:

* ``instagram_recent_posts``  — what is already on the feed, with captions and
  counts. Stops the agent repeating a post, and gives it its own back catalogue
  as context.
* ``instagram_comments`` / ``instagram_reply_to_comment`` /
  ``instagram_moderate_comment`` — read and answer the audience; hide or delete
  abuse.
* ``instagram_insights`` — how a post or the account performed, so the brief
  "post what works" has something to work from.
* ``instagram_publishing_limit`` — how much of the rolling 24h quota is left.
  Worth checking BEFORE spending a Sora clip on a post that cannot go out for
  another twenty hours.
* ``instagram_profile`` — follower/media counts and the bio.
* ``instagram_mentions`` — posts that tagged this account.

Every factory returns ``None`` unless the run targets an Instagram account, so a
TikTok run is not handed nine irrelevant tools. Reads are cheap; the write tools
(reply, moderate) act on the real account immediately and are **not** gated by
``publish_mode`` — that gate is about posting. The prompt tells the agent to
reply in the account's voice and never to argue with users.
"""
from __future__ import annotations

import logging

from agents import function_tool

from .. import tokens
from ..models import PlatformName
from .registry import register_tool

logger = logging.getLogger("aismm.tools.instagram")


async def _instagram_context(state: dict):
    """Return ``(platform, account, token)`` for an Instagram run, else ``None``."""
    account = state.get("account")
    if account is None or account.platform is not PlatformName.instagram:
        return None
    from ..platforms.registry import get_platform  # lazy

    token = await tokens.valid_access_token(account, state["store"])
    if not token:
        return None
    return get_platform(PlatformName.instagram), account, token


def _guard(state: dict):
    """Factory helper: only build these tools for an Instagram run."""
    account = state.get("account")
    return account is not None and account.platform is PlatformName.instagram


async def _with_context(state: dict, call):
    context = await _instagram_context(state)
    if context is None:
        return {"error": "not_available",
                "message": "This run does not target a connected Instagram account."}
    platform, account, token = context
    try:
        return await call(platform, account, token)
    except Exception as exc:  # noqa: BLE001 - report, never kill the run
        logger.warning("Instagram tool failed: %s", exc)
        return {"error": "instagram_api_error", "message": str(exc)}


def _make_recent_posts(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def instagram_recent_posts(limit: int = 10) -> dict:
        """List this account's recent Instagram posts, with captions and counts.

        Use it to see what has already been published before choosing a topic —
        the fastest way to avoid repeating yourself — and to learn the account's
        established voice from its own back catalogue.

        Args:
            limit: How many posts to return (1–100, newest first).
        """
        async def call(platform, account, token):
            posts = await platform.list_media(token, account, limit=limit)
            return {"count": len(posts), "posts": [
                {"id": p.get("id"), "caption": (p.get("caption") or "")[:600],
                 "type": p.get("media_product_type") or p.get("media_type"),
                 "permalink": p.get("permalink"), "posted_at": p.get("timestamp"),
                 "likes": p.get("like_count"), "comments": p.get("comments_count")}
                for p in posts]}

        return await _with_context(state, call)

    return instagram_recent_posts


def _make_comments(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def instagram_comments(media_id: str, limit: int = 25) -> dict:
        """Read the comments on one of this account's posts, with their replies.

        Args:
            media_id: A post id from ``instagram_recent_posts``.
            limit: How many comments to return (1–100).
        """
        async def call(platform, account, token):
            comments = await platform.list_comments(token, media_id, limit=limit)
            return {"media_id": media_id, "count": len(comments), "comments": [
                {"id": c.get("id"), "text": c.get("text"), "from": c.get("username"),
                 "at": c.get("timestamp"), "likes": c.get("like_count"),
                 "hidden": c.get("hidden"),
                 "replies": [{"id": r.get("id"), "text": r.get("text"),
                              "from": r.get("username")}
                             for r in (c.get("replies", {}) or {}).get("data", [])]}
                for c in comments]}

        return await _with_context(state, call)

    return instagram_comments


def _make_reply(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def instagram_reply_to_comment(comment_id: str, message: str) -> dict:
        """Reply publicly to a comment, in the account's voice.

        This posts immediately and is visible to everyone — it is not covered by
        the instruction's publish mode, which governs posts. Be helpful and brief,
        stay on-brief, never argue, and do not promise anything on the brand's
        behalf. If a comment is abusive or spam, use
        ``instagram_moderate_comment`` instead of replying.

        Args:
            comment_id: From ``instagram_comments``.
            message: The reply text.
        """
        async def call(platform, account, token):
            result = await platform.reply_to_comment(token, comment_id, message)
            logger.info("Replied to Instagram comment %s (%d chars)", comment_id, len(message))
            return {"status": "replied", "reply_id": result.get("id")}

        return await _with_context(state, call)

    return instagram_reply_to_comment


def _make_moderate(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def instagram_moderate_comment(comment_id: str, action: str = "hide") -> dict:
        """Hide, unhide, or delete a comment on this account's post.

        Prefer "hide" — it removes the comment from public view without
        destroying it, so a human can review the decision. "delete" is
        irreversible; use it only for content the brief clearly forbids.

        Args:
            comment_id: From ``instagram_comments``.
            action: "hide", "unhide", or "delete".
        """
        async def call(platform, account, token):
            verb = (action or "hide").lower()
            if verb == "delete":
                await platform.delete_comment(token, comment_id)
            elif verb in {"hide", "unhide"}:
                await platform.set_comment_hidden(token, comment_id, hidden=verb == "hide")
            else:
                return {"error": "bad_action",
                        "message": f"{action!r} is not one of hide / unhide / delete."}
            logger.info("Moderated Instagram comment %s: %s", comment_id, verb)
            return {"status": verb, "comment_id": comment_id}

        return await _with_context(state, call)

    return instagram_moderate_comment


def _make_insights(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def instagram_insights(media_id: str = "", metrics: str = "") -> dict:
        """How a post — or the account — performed.

        Pass a ``media_id`` for one post's metrics, or omit it for account-level
        metrics. Use this to ground "post more of what works" in actual numbers
        rather than a guess.

        Args:
            media_id: Post id from ``instagram_recent_posts``; empty for the account.
            metrics: Optional comma-separated override. Meta retires metric names
                periodically (``impressions`` and ``profile_views`` among them),
                so if a name is rejected the error names it — pick another.
        """
        async def call(platform, account, token):
            if media_id:
                rows = await platform.media_insights(token, media_id, metrics)
            else:
                rows = await platform.account_insights(token, account, metrics=metrics)
            return {"scope": "media" if media_id else "account", "metrics": [
                {"name": row.get("name"),
                 "value": (row.get("total_value") or {}).get("value")
                 if row.get("total_value") else
                 (row.get("values") or [{}])[0].get("value")}
                for row in rows]}

        return await _with_context(state, call)

    return instagram_insights


def _make_publishing_limit(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def instagram_publishing_limit() -> dict:
        """How much of Instagram's rolling 24-hour publishing quota is left.

        Check this BEFORE generating expensive media if you suspect the account
        has been busy: Instagram caps API-published posts per 24 hours (a carousel
        counts as one), and a container you cannot publish is wasted work. If the
        quota is exhausted, finish with ``report_failure`` rather than posting.
        """
        async def call(platform, account, token):
            limit = await platform.publishing_limit(token, account)
            used = limit.get("quota_usage")
            config = limit.get("config") or {}
            total = config.get("quota_total")
            remaining = (total - used) if isinstance(total, int) and isinstance(used, int) else None
            return {"used": used, "quota_total": total, "remaining": remaining,
                    "window_hours": config.get("quota_duration", 86400) // 3600
                    if isinstance(config.get("quota_duration"), int) else 24}

        return await _with_context(state, call)

    return instagram_publishing_limit


def _make_profile(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def instagram_profile() -> dict:
        """This account's username, bio, follower/following and post counts."""
        async def call(platform, account, token):
            profile = await platform.account_profile(token, account)
            return {k: profile.get(k) for k in
                    ("username", "name", "biography", "followers_count",
                     "follows_count", "media_count")}

        return await _with_context(state, call)

    return instagram_profile


def _make_mentions(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def instagram_mentions(limit: int = 10) -> dict:
        """Posts by others that tagged this account — worth engaging with.

        Args:
            limit: How many to return (1–50).
        """
        async def call(platform, account, token):
            tagged = await platform.list_mentions(token, account, limit=limit)
            return {"count": len(tagged), "posts": [
                {"id": p.get("id"), "caption": (p.get("caption") or "")[:400],
                 "permalink": p.get("permalink"), "at": p.get("timestamp")}
                for p in tagged]}

        return await _with_context(state, call)

    return instagram_mentions


register_tool("instagram_recent_posts", _make_recent_posts)
register_tool("instagram_comments", _make_comments)
register_tool("instagram_reply_to_comment", _make_reply)
register_tool("instagram_moderate_comment", _make_moderate)
register_tool("instagram_insights", _make_insights)
register_tool("instagram_publishing_limit", _make_publishing_limit)
register_tool("instagram_profile", _make_profile)
register_tool("instagram_mentions", _make_mentions)
