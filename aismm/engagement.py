"""``perform_reply`` — the mode-gated engagement action.

The sibling of [publish_tool.perform_publish](tools/publish_tool.py), one level
over: where that gates *posting a new post*, this gates *replying to a comment /
mention*. An engagement run's reply tools call this instead of hitting the
platform directly, so a reply obeys the same three-way ``publish_mode`` gate a
post does:

* ``dry_run``  -> StagedPost(action_type="reply", preview); never touch the API.
* ``approval`` -> StagedPost(action_type="reply", pending_approval); a human
                  clicks Approve, which sends the reply (orchestrator.approve_staged).
* ``live``     -> send the reply now via the platform integration.

Two guards run before anything is staged or sent, because a cron engagement run
re-reads the same thread every time it fires:

* the [engagement ledger](engagement_ledger.py) — a target already REPLIED to;
* an already-OPEN staged reply for the same target (a dry-run preview or a
  pending-approval item from an earlier run) — so the queue doesn't fill with
  duplicates of the same unanswered comment.

Unlike ``perform_publish`` this is NOT a terminal action: an engage run answers
several targets and then ends with ``finish_engagement``. So ``perform_reply``
accumulates counts on ``state["engagement"]`` and appends to the run log, but it
does not set ``state["result"]``.

Platform / store imports are lazy to keep this decoupled, matching publish_tool.
"""
from __future__ import annotations

import logging

from . import cooldown, engagement_ledger, tokens
from .models import PublishMode, StagedPost, StagedStatus

logger = logging.getLogger("aismm.engagement")

# A reply refused for volume reasons still means the platform is throttling this
# account; back off like a rate-limited post does.
RATE_LIMIT_COOLDOWN_SECONDS = 3600

# Staged replies still "in the queue" for a target — a new run must not re-stage
# the same comment. A rejected or (separately, via the ledger) sent reply is not
# here, so a rejected reply can be reconsidered next run.
_OPEN_STAGED = (StagedStatus.preview, StagedStatus.pending_approval, StagedStatus.approved)


def _counters(state: dict) -> dict:
    """Per-run engagement tally, read by finish_engagement to set the run status."""
    tally = state.get("engagement")
    if not isinstance(tally, dict):
        tally = {"replied": 0, "staged": 0, "skipped": 0, "failed": 0,
                 "targets": [], "failures": []}
        state["engagement"] = tally
    # Older tallies (or ones seeded elsewhere) may predate the failure fields.
    tally.setdefault("failed", 0)
    tally.setdefault("failures", [])
    return tally


async def perform_reply(state: dict, *, target_type: str, target_id: str, text: str,
                        target_excerpt: str = "", reply_to: str = "") -> dict:
    """Gate + stage/send one reply. Returns a status dict for the agent.

    ``target_type`` is ``comment`` / ``mention`` / ``reply`` / ``dm``.
    ``target_id`` is the platform id of the thing being answered — for a DM that
    is the inbound MESSAGE id, which is what the ledger dedupes on (reply once per
    message). ``text`` is the reply the agent wrote. ``reply_to`` is the SEND
    destination when it differs from ``target_id`` — for a DM the conversation /
    recipient id — and is empty for a comment (you reply to the comment id).
    """
    account = state["account"]
    instruction = state["instruction"]
    store = state["store"]
    run = state["run"]
    tally = _counters(state)

    text = (text or "").strip()
    target_id = str(target_id or "").strip()
    reply_to = str(reply_to or "").strip()
    target_type = (target_type or "comment").strip().lower()
    if not target_id:
        return {"error": "no_target", "message": "Pass the id of the comment/message to reply to."}
    if not text:
        return {"error": "empty_reply", "message": "The reply text is empty — write a reply first."}

    from .platforms.registry import get_platform  # lazy

    platform = get_platform(account.platform)
    # A DM is gated by its own capability; a comment/mention/reply by supports_comments.
    if target_type == "dm":
        if not platform.capabilities.supports_dms:
            return {"error": "unsupported",
                    "message": f"{account.platform.value} does not support direct messages here."}
    elif not platform.capabilities.supports_comments:
        return {"error": "unsupported",
                "message": f"{account.platform.value} does not support replying to comments here."}

    # --- guard 1: already replied (sent) ---------------------------------- #
    if engagement_ledger.answered(account, target_type, target_id):
        tally["skipped"] += 1
        logger.info("Skipping %s %s — already answered by %s", target_type, target_id,
                    account.handle or account.external_id)
        return {"status": "skipped", "already_answered": True,
                "message": ("This account already replied to that item — do not answer it "
                            "again. Move to the next unanswered one.")}

    # --- guard 2: already queued (staged, not yet sent) ------------------- #
    if _has_open_staged_reply(store, account.id, target_type, target_id):
        tally["skipped"] += 1
        return {"status": "skipped", "already_staged": True,
                "message": ("A reply to that item is already waiting in the approval/preview "
                            "queue from an earlier run — do not stage it again.")}

    mode: PublishMode = instruction.publish_mode
    staged = StagedPost(
        instruction_id=instruction.id, account_id=account.id, run_id=run.id,
        workspace_id=instruction.workspace_id,
        caption=text, media_kind="text",
        action_type="reply", target_type=target_type, target_id=target_id,
        target_conversation=reply_to, target_excerpt=(target_excerpt or "")[:500],
    )

    # --- dry-run: preview only -------------------------------------------- #
    if mode == PublishMode.dry_run:
        staged.status = StagedStatus.preview
        store.add_staged(staged)
        tally["staged"] += 1
        run.log = (run.log + f"\nDRY-RUN staged reply to {target_type} {target_id}.").strip()
        store.update_run(run)
        return {"status": "staged", "mode": "dry_run", "staged_id": staged.id,
                "message": "Prepared a dry-run preview of this reply. Nothing was sent."}

    # --- approval: queue for a human click -------------------------------- #
    if mode == PublishMode.approval:
        staged.status = StagedStatus.pending_approval
        store.add_staged(staged)
        tally["staged"] += 1
        run.log = (run.log + f"\nQueued reply to {target_type} {target_id} for approval.").strip()
        store.update_run(run)
        return {"status": "pending_approval", "mode": "approval", "staged_id": staged.id,
                "message": "Queued this reply for approval. It sends when approved in the dashboard."}

    # --- live: send now --------------------------------------------------- #
    try:
        access_token = await tokens.valid_access_token(account, store)
        if not access_token:
            raise RuntimeError("account has no stored access token — reconnect it in the dashboard.")
        result = await platform.reply_to_target(
            access_token, account, target_type=target_type, target_id=target_id,
            text=text, reply_to=reply_to)
    except Exception as exc:  # noqa: BLE001 - surface the platform's own message
        # A volume refusal (Instagram RateLimited) pauses the account, like a post.
        from .platforms.instagram import RateLimited
        if isinstance(exc, RateLimited):
            held = cooldown.start(account, store, exc.retry_after_seconds,
                                  reason=f"{account.platform.value} rate limit (reply)")
            message = (f"{exc} — {account.platform.value} is refusing replies for volume "
                       f"reasons. Paused for {held // 60} minutes.")
            logger.error("Reply rate-limited: %s", message)
            _record_failure(tally, run, store, target_type, target_id, message)
            return {"error": "rate_limited", "message": message, "retry_after_minutes": held // 60}
        logger.exception("Live reply failed for %s (%s %s)",
                         account.handle or account.external_id, target_type, target_id)
        _record_failure(tally, run, store, target_type, target_id, str(exc))
        return {"error": "reply_failed", "message": str(exc)}

    url = result.get("url", "") if isinstance(result, dict) else ""
    engagement_ledger.record(account, store, target_type, target_id,
                             url=url, instruction_id=instruction.id)
    staged.status = StagedStatus.published
    staged.external_url = url
    store.add_staged(staged)
    tally["replied"] += 1
    tally["targets"].append(target_id)
    run.log = (run.log + f"\nReplied to {target_type} {target_id}"
               + (f": {url}" if url else "")).strip()
    store.update_run(run)
    return {"status": "replied", "mode": "live", "url": url,
            "message": "Reply sent. Move to the next unanswered item, or finish_engagement."}


def _record_failure(tally: dict, run, store, target_type: str, target_id: str,
                    reason: str) -> None:
    """Count a reply the platform REFUSED (403, rate limit, …) on the run tally.

    Without this a blocked reply left the tally at ``0 replied / 0 staged / 0
    skipped``, so ``finish_engagement`` reported the run as ``skipped`` —
    indistinguishable from "nothing new to answer" — even though it tried and was
    refused. The reason (kept short) lets ``finish_engagement`` fail the run with
    the actual blocker instead of a silent skip.
    """
    tally["failed"] = int(tally.get("failed", 0)) + 1
    reason = (reason or "").strip()
    if reason:
        tally.setdefault("failures", []).append(f"{target_type} {target_id}: {reason}"[:300])
    run.log = (run.log + f"\nReply to {target_type} {target_id} BLOCKED: {reason}").strip()
    store.update_run(run)


def _has_open_staged_reply(store, account_id: str, target_type: str, target_id: str) -> bool:
    """Is there already a preview/pending/approved reply staged for this target?

    Uses the store's dedicated lookup when available (SQL on LocalStore); falls
    back to scanning ``list_staged`` so a backend without the helper still works.
    """
    wanted = engagement_ledger.key(target_type, target_id)
    finder = getattr(store, "open_staged_reply_keys", None)
    if callable(finder):
        try:
            return wanted in set(finder(account_id))
        except Exception as exc:  # noqa: BLE001 - never block a reply on the guard
            logger.warning("open_staged_reply_keys failed (%s); scanning instead", exc)
    for staged in store.list_staged(pending_only=False, limit=500):
        if (staged.account_id == account_id and staged.action_type == "reply"
                and staged.status in _OPEN_STAGED
                and engagement_ledger.key(staged.target_type, staged.target_id) == wanted):
            return True
    return False
