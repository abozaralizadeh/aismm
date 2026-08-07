"""YouTube-specific engagement tools: read comment threads, reply to them.

The counterpart of ``instagram_tools`` / ``twitter_tools`` for YouTube. Publishing
a video still goes through the one gated ``publish`` tool; this module is what an
ENGAGEMENT run uses to read the comment threads on the channel's videos and answer
them.

Every factory returns ``None`` unless the run targets a YouTube account, so an
Instagram run is not handed them. Replies go out through the instruction's publish
mode (``engagement.perform_reply``), like every other reply — ``dry_run`` stages a
preview, ``approval`` queues it, ``live`` sends it now.

Reading and replying both need the ``youtube.force-ssl`` scope; an account
connected before that scope was added must be reconnected, and the tool will
surface the API's permission error until it is.
"""
from __future__ import annotations

import logging

from agents import function_tool

from .. import engagement, engagement_ledger, tokens
from ..models import PlatformName
from .registry import register_tool

logger = logging.getLogger("aismm.tools.youtube")

# YouTube's answerable unit is a top-level comment; key the ledger on it so the
# read tool's already_answered flag and the reply's recorded fingerprint match.
_YT_TARGET = "comment"


def _guard(state: dict) -> bool:
    account = state.get("account")
    return account is not None and account.platform is PlatformName.youtube


async def _context(state: dict):
    account = state.get("account")
    if account is None or account.platform is not PlatformName.youtube:
        return None
    from ..platforms.registry import get_platform  # lazy

    token = await tokens.valid_access_token(account, state["store"])
    if not token:
        return None
    return get_platform(PlatformName.youtube), account, token


async def _with_context(state: dict, call):
    context = await _context(state)
    if context is None:
        return {"error": "not_available",
                "message": "This run does not target a connected YouTube account."}
    platform, account, token = context
    try:
        return await call(platform, account, token)
    except Exception as exc:  # noqa: BLE001 - report, never kill the run
        logger.warning("YouTube tool failed: %s", exc)
        return {"error": "youtube_api_error", "message": str(exc)}


def _make_comment_threads(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def youtube_comments(limit: int = 20) -> dict:
        """Recent comment threads across this channel's videos, newest first.

        Read this on an engagement run to find comments to answer, then reply with
        ``youtube_reply_to_comment`` using each item's ``id``. Items flagged
        ``already_answered`` have been handled — skip them.

        Args:
            limit: How many threads to return (1–100).
        """
        async def call(platform, account, token):
            threads = await platform.list_comment_threads(token, account, limit=limit)
            for t in threads:
                t["already_answered"] = engagement_ledger.answered(
                    account, _YT_TARGET, t.get("id"))
            return {"count": len(threads), "comments": threads}

        return await _with_context(state, call)

    return youtube_comments


def _make_reply(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def youtube_reply_to_comment(comment_id: str, message: str,
                                       comment_excerpt: str = "") -> dict:
        """Reply to a comment on one of this channel's videos, in its voice.

        The reply goes out through the instruction's publish mode: ``dry_run``
        stages a preview, ``approval`` queues it for a human, ``live`` sends it
        now. A result of "staged"/"pending_approval" means it did its job. Be
        brief and helpful, never argue. A comment already answered (or queued)
        comes back "skipped" — move on.

        Args:
            comment_id: A top-level comment ``id`` from ``youtube_comments``.
            message: The reply text.
            comment_excerpt: The comment you are answering, so a human reviewing
                the queue can see what the reply responds to.
        """
        if not _guard(state):
            return {"error": "not_available",
                    "message": "This run does not target a connected YouTube account."}
        return await engagement.perform_reply(
            state, target_type=_YT_TARGET, target_id=comment_id, text=message,
            target_excerpt=comment_excerpt)

    return youtube_reply_to_comment


register_tool("youtube_comments", _make_comment_threads)
register_tool("youtube_reply_to_comment", _make_reply)
