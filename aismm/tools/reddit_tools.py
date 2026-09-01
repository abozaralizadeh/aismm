"""Reddit-specific tools: find other redditors' posts and comment on them.

Reddit is an OUTREACH surface first — topical subreddits are exactly where an
account's ideal followers already gather, which is why an operator's outreach
targets so often name ``r/...``. ``reddit_search_posts`` finds recent submissions
by keyword and/or subreddit; ``reddit_reply`` comments on one through the SAME
mode gate a post uses (dry_run previews, approval queues, live sends) — a comment
is outbound content, so it is gated exactly like a reply on any other platform.

Every factory returns ``None`` unless the run targets a connected Reddit account,
so an X or Instagram run is not handed these. Reddit exposes no like-a-post API we
use here (voting needs a scope the account is not connected with), so there is no
like tool — engagement is search + comment.
"""
from __future__ import annotations

import logging

from agents import function_tool

from .. import engagement, engagement_ledger, tokens
from ..models import PlatformName
from .registry import register_tool

logger = logging.getLogger("aismm.tools.reddit")

# One canonical target kind for every submission the account might comment on, so
# the search tool's already_answered flag and the reply tool's recorded
# fingerprint line up and dedup works. Comment-on-comment would be "comment", but
# outreach engages submissions.
_RD_TARGET = "submission"
# A private message is a separate target kind: the ledger keys on the inbound
# message fullname, which never collides with a submission fullname.
_RD_DM = "dm"

# Cap how many subreddits one run fans out over — each is a live API call, and the
# agent should engage the best few, not sweep everything.
_MAX_SUBREDDITS = 5


def _guard(state: dict) -> bool:
    """Factory helper: only build these tools for a Reddit run."""
    account = state.get("account")
    return account is not None and account.platform is PlatformName.reddit


async def _context(state: dict):
    account = state.get("account")
    if account is None or account.platform is not PlatformName.reddit:
        return None
    from ..platforms.registry import get_platform  # lazy

    token = await tokens.valid_access_token(account, state["store"])
    if not token:
        return None
    return get_platform(PlatformName.reddit), account, token


async def _with_context(state: dict, call):
    context = await _context(state)
    if context is None:
        return {"error": "not_available",
                "message": "This run does not target a connected Reddit account."}
    platform, account, token = context
    try:
        return await call(platform, account, token)
    except Exception as exc:  # noqa: BLE001 - report, never kill the run
        logger.warning("Reddit tool failed: %s", exc)
        return {"error": "reddit_api_error", "message": str(exc)}


def _annotate(items: list[dict], account) -> list[dict]:
    for item in items:
        item["already_answered"] = engagement_ledger.answered(
            account, _RD_TARGET, item.get("id"))
    return items


def _make_search(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def reddit_search_posts(query: str = "", subreddit: str = "",
                                  limit: int = 10) -> dict:
        """Find OTHER redditors' recent submissions to engage — the OUTREACH search.

        With no arguments it searches the instruction's configured outreach
        targets: it browses each target subreddit's newest posts (and, when
        keywords are set too, searches those within the subreddit) and runs one
        site-wide keyword search. Pass ``subreddit`` and/or ``query`` to search
        something specific you inferred from the brief. Results are de-duplicated
        and newest-first; NSFW and this account's own posts are excluded. Items
        flagged ``already_answered`` have been engaged — skip them. Comment on one
        with ``reddit_reply``.

        Args:
            query: Keyword/phrase to search for. Empty = use the instruction's targets.
            subreddit: Restrict to one subreddit (``r/foo`` or ``foo``). Empty =
                site-wide, or every target subreddit when ``query`` is also empty.
            limit: Roughly how many posts to return (newest first).
        """
        searches = _plan_searches(state, query, subreddit)
        if not searches:
            return {"error": "no_targets",
                    "message": ("No query or subreddit given and the instruction has no "
                                "outreach targets. Infer a keyword or subreddit from the "
                                "brief and pass it as `query` or `subreddit`.")}

        async def call(platform, account, token):
            seen: set[str] = set()
            posts: list[dict] = []
            for q, sub in searches:
                found = await platform.search_content(
                    token, account, query=q, subreddit=sub, limit=limit)
                for p in found:
                    pid = p.get("id")
                    if pid and pid not in seen:
                        seen.add(pid)
                        posts.append(p)
            posts = _annotate(posts, account)[:limit]
            return {"searches": [{"query": q, "subreddit": sub} for q, sub in searches],
                    "count": len(posts), "posts": posts}

        return await _with_context(state, call)

    return reddit_search_posts


def _plan_searches(state: dict, query: str, subreddit: str) -> list[tuple[str, str]]:
    """Work out the (query, subreddit) searches to run for this call.

    An explicit ``query``/``subreddit`` is one search, verbatim. Otherwise derive
    them from the instruction's targets: browse each target subreddit (filtered by
    the keyword query when there is one), plus one site-wide keyword search.
    """
    query = (query or "").strip()
    subreddit = (subreddit or "").strip()
    if query or subreddit:
        return [(query, subreddit)]

    from ..targets import reddit_query

    instruction = state.get("instruction")
    targets = instruction.parsed_targets if instruction is not None else None
    if not targets:
        return []
    kw_query = reddit_query(targets)
    searches: list[tuple[str, str]] = [(kw_query, sub)
                                       for sub in targets.subreddits[:_MAX_SUBREDDITS]]
    if kw_query:
        searches.append((kw_query, ""))       # one site-wide keyword search too
    return searches


def _make_reply(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def reddit_reply(post_id: str, text: str, replying_to: str = "") -> dict:
        """Comment on someone else's Reddit submission, in the account's voice.

        The comment goes out through the instruction's publish mode, exactly like a
        post: ``dry_run`` stages a preview, ``approval`` queues it for a human, and
        only ``live`` sends it now. A result of "staged"/"pending_approval" means
        it did its job. Add genuine value — answer a question, share something
        relevant; never advertise or argue. A submission already commented on (or
        already queued) comes back "skipped" — move on.

        Args:
            post_id: The submission's id from ``reddit_search_posts`` (the ``t3_``
                fullname). Pass it exactly as given.
            text: The comment, in Markdown.
            replying_to: The title/text of the submission you are answering, so a
                human reviewing the queue can see what the comment responds to.
        """
        if not _guard(state):
            return {"error": "not_available",
                    "message": "This run does not target a connected Reddit account."}
        return await engagement.perform_reply(
            state, target_type=_RD_TARGET, target_id=post_id, text=text,
            target_excerpt=replying_to)

    return reddit_reply


def _make_dms(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def reddit_dms(limit: int = 25) -> dict:
        """Recent INBOUND private messages to this account — PMs to answer.

        Reads the Reddit inbox and returns true private messages (comment replies
        show up in ``reddit_search_posts``/the comment flow, not here). Each item
        carries the ``id`` (the message fullname — pass it back to
        ``reddit_reply_to_dm`` as ``message_id``), ``sender`` and ``text``. Items
        flagged ``already_answered`` have been handled — skip them. Needs the
        ``privatemessages`` scope; an account connected before it was added must be
        reconnected. Best-effort: an empty list can mean "no new PMs" or "the scope
        is missing".

        Args:
            limit: How many messages to return (1–100, newest first).
        """
        async def call(platform, account, token):
            engagement.note_read(state, "reddit_dms")
            dms = await platform.list_dms(token, account, limit=limit)
            for d in dms:
                d["already_answered"] = engagement_ledger.answered(
                    account, _RD_DM, d.get("id"))
            logger.info("DMs read for %s: %d inbound message(s), %d unanswered",
                        account.handle or account.external_id, len(dms),
                        sum(1 for d in dms if not d.get("already_answered")))
            return {"count": len(dms), "dms": dms}

        return await _with_context(state, call)

    return reddit_dms


def _make_reply_to_dm(state: dict):
    if not _guard(state):
        return None

    @function_tool
    async def reddit_reply_to_dm(message_id: str, text: str, replying_to: str = "") -> dict:
        """Answer a private message, in the account's voice.

        The reply goes out through the instruction's publish mode, exactly like a
        comment: ``dry_run`` stages a preview, ``approval`` queues it for a human,
        only ``live`` sends it now. A PM already answered (or already queued) comes
        back "skipped" — move on. Be helpful and genuine; never send unsolicited
        messages — only answer what arrived.

        Args:
            message_id: The message's ``id`` (the ``t4_`` fullname from
                ``reddit_dms``). Pass it exactly as given — it is both what is
                deduped and where the reply is delivered.
            text: The reply, in Markdown.
            replying_to: The text of the PM you are answering, shown to a human
                reviewing the queue.
        """
        if not _guard(state):
            return {"error": "not_available",
                    "message": "This run does not target a connected Reddit account."}
        return await engagement.perform_reply(
            state, target_type=_RD_DM, target_id=message_id, text=text,
            target_excerpt=replying_to)

    return reddit_reply_to_dm


register_tool("reddit_search_posts", _make_search)
register_tool("reddit_reply", _make_reply)
register_tool("reddit_dms", _make_dms)
register_tool("reddit_reply_to_dm", _make_reply_to_dm)
