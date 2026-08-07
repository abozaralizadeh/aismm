"""Orchestration: turn a fired Instruction into runs, one per selected account.

Each (instruction, account) pair is guarded by a single-flight lock (so a double
schedule fire never double-posts), gets a ``Run`` row, and is handed to the
autonomous agent. Also exposes ``approve_staged`` — the dashboard's Approve button
for approval-mode posts — which performs the actual live publish.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import cooldown, engagement_ledger, logging_setup, publish_ledger, tokens
from .config import settings
from .agent import run_for_account
from .assets import exists as asset_exists
from .assets import kind_from_path
from .models import (
    Account, Instruction, PublishMode, Run, RunStatus, StagedPost, StagedStatus,
)
from .platforms.instagram import RateLimited
from .platforms.registry import get_platform
from .store import get_store
from .tools.publish_tool import _confirm_duplicate

logger = logging.getLogger("aismm.orchestrator")

# The lock is HEARTBEATED for as long as the run is alive, so this is "how long
# after its owner dies before the lock is reclaimable", not "how long a run may
# take". It used to be 30 minutes with no heartbeat: a dashboard "Run now" thread
# killed by a gunicorn restart left its lock behind, and every scheduled run of
# that instruction was skipped as "already running" for the next half hour —
# the run had finished (violently), but the lock hadn't heard.
_LOCK_TTL = 300
_LOCK_HEARTBEAT = 60

# A wedged run must not hold a scheduler thread forever. APScheduler runs jobs in
# a 10-thread pool with max_instances=1, so one run that never returns silences
# that instruction permanently and leaks a pool thread; ten of them stop every
# instruction. Nothing in a run legitimately takes this long.
RUN_TIMEOUT_SECONDS = settings.run_timeout_seconds


def _run_async(coro):
    """Run an async coroutine from a sync context (scheduler thread / CLI)."""
    return asyncio.run(coro)


async def _with_timeout(coro, seconds: int = 0):
    """Abandon a run that has stopped making progress.

    ``asyncio.TimeoutError`` is ``TimeoutError`` on 3.11+; normalized here so the
    caller can catch the builtin on every supported version.
    """
    limit = seconds or RUN_TIMEOUT_SECONDS
    if limit <= 0:
        return await coro                 # RUN_TIMEOUT_SECONDS=0 disables the ceiling
    try:
        return await asyncio.wait_for(coro, timeout=limit)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(str(exc) or "run timed out") from None


class _LockHeartbeat:
    """Keeps a run's lock fresh while the run is alive; stops on exit.

    A daemon thread, so it can never keep the process up on its own — if
    everything else is gone, the lock simply stops being renewed and goes stale.
    """

    def __init__(self, store, key: str, interval: int = _LOCK_HEARTBEAT):
        self._store, self._key, self._interval = store, key, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._beat, name=f"lock-heartbeat:{self._key}",
                                        daemon=True)
        self._thread.start()
        return self

    def _beat(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._store.touch_lock(self._key):
                    logger.warning("Lock %s vanished while its run was still going", self._key)
                    return
            except Exception as exc:  # noqa: BLE001 - never kill a run over bookkeeping
                logger.warning("Could not refresh lock %s: %s", self._key, exc)

    def __exit__(self, *exc_info):
        self._stop.set()
        return False


def run_instruction(instruction_id: str) -> list[dict]:
    """Execute an instruction against all its selected accounts. Returns per-account results."""
    store = get_store()
    instruction = store.get_instruction(instruction_id)
    if not instruction:
        logger.warning("run_instruction: unknown instruction %s", instruction_id)
        return []
    if not instruction.enabled:
        logger.info("run_instruction: %s is disabled; skipping", instruction.name)
        return []

    results: list[dict] = []
    for account_id in instruction.account_ids:
        account = store.get_account(account_id)
        if not account:
            logger.warning("Instruction %s references missing account %s", instruction.name, account_id)
            continue
        results.append(_run_one(instruction, account, store))
    return results


def run_single(instruction: Instruction, account: Account) -> dict:
    """Run one (instruction, account) pair directly (used by the CLI 'post' command)."""
    return _run_one(instruction, account, get_store())


def republish_run(run_id: str, caption: str = "") -> dict:
    """Publish a failed run's media AGAIN, without re-running the agent.

    The common retry is not "think about this afresh" — it is "the post was
    fine, the publish failed". A rate limit, an expired token, X running out of
    API credits. Re-running the agent for that regenerates a Sora clip or a
    gpt-image-2 render, costs minutes and money, and produces *different* content
    than the one that was reviewed.

    So this takes the exact caption, media and placement the failed run recorded
    and sends them straight to ``perform_publish`` — the same gate, the same
    disclosure, the same duplicate guard, no model call anywhere. ``caption``
    overrides the recorded one if the wording was the problem.
    """
    store = get_store()
    original = store.get_run(run_id)
    if not original:
        return {"error": "not_found"}
    instruction = store.get_instruction(original.instruction_id)
    account = store.get_account(original.account_id)
    if not instruction:
        return {"error": "instruction_missing",
                "message": "The instruction this run belonged to has been deleted."}
    if not account:
        return {"error": "account_missing",
                "message": "The account this run targeted has been disconnected."}

    paths = [p for p in original.asset_paths if p]
    text = (caption or original.caption or "").strip()
    if not paths and not text:
        return {"error": "nothing_to_publish",
                "message": ("This run recorded no caption or media — there is nothing to "
                            "send again. Use the agent retry instead.")}

    missing = [p for p in paths if not asset_exists(p)]
    if missing:
        return {"error": "media_gone",
                "message": (f"{len(missing)} of {len(paths)} asset(s) from that run no longer "
                            f"exist, so it cannot be published as-is. Use the agent retry, "
                            f"which produces the media again.")}

    if instruction.publish_mode is PublishMode.live and cooldown.is_active(account):
        return {"error": "rate_limited",
                "message": (f"{account.handle or account.external_id} is still in a publishing "
                            f"cooldown for {cooldown.describe(account)}. Clear it or wait.")}

    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            workspace_id=instruction.workspace_id,
                            status=RunStatus.running,
                            prompt=f"(republish of run {run_id[:8]} — no agent involved)"))
    log_token = logging_setup.current_run_id.set(run.id[:8])
    try:
        logger.info("REPUBLISH %s | from run %s | %d asset(s), placement=%s, caption %d chars",
                    run.id[:8], run_id[:8], len(paths), original.placement, len(text))
        state = {"account": account, "instruction": instruction, "store": store,
                 "run": run, "assets": []}
        # The media is already normalized (perform_publish converted it before the
        # failed attempt), so this re-runs the gate, not the generation.
        from .tools.publish_tool import perform_publish

        result = _run_async(perform_publish(
            state, text, asset_path=paths[0] if paths else "",
            media_kind=kind_from_path(paths[0]) if paths else "text",
            asset_paths=paths if len(paths) > 1 else None,
            placement=original.placement or "feed"))
        logger.info("REPUBLISH DONE %s | %s", run.id[:8], result)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("Republish of %s failed", run_id[:8])
        run.status = RunStatus.failed
        run.error = str(exc)
        store.update_run(run)
        return {"error": "publish_failed", "message": str(exc)}
    finally:
        logging_setup.current_run_id.reset(log_token)


# A run that outlives this is not slow, it is gone: the in-process ceiling would
# have ended it, so only a process that DIED without unwinding leaves one behind.
_REAP_GRACE_SECONDS = 900
# Used when the run ceiling is disabled (RUN_TIMEOUT_SECONDS=0) and there is
# therefore no bound to derive one from.
_REAP_FALLBACK_SECONDS = 86400


def stale_run_cutoff(older_than_seconds: int = 0) -> int:
    """How old a ``running`` run must be before it is certainly abandoned."""
    if older_than_seconds > 0:
        return older_than_seconds
    ceiling = RUN_TIMEOUT_SECONDS
    return ceiling + _REAP_GRACE_SECONDS if ceiling > 0 else _REAP_FALLBACK_SECONDS


def reap_stale_runs(store=None, *, older_than_seconds: int = 0, apply: bool = True) -> list:
    """Mark abandoned ``running`` runs as failed. Returns the runs it found.

    A run is only ever moved off ``running`` by the code that is executing it —
    so when the process dies mid-run (a gunicorn restart, an OOM kill, a deploy)
    the row says "running" forever. The lock it held is heartbeated and clears
    itself within one TTL, so the *instruction* recovers; the run row does not,
    and the Runs page fills with runs that will never finish.

    Age is the signal, and it is a safe one: a live run cannot be older than
    ``RUN_TIMEOUT_SECONDS`` because that ceiling would have ended it, so anything
    past the ceiling plus a grace period has no process behind it. ``apply=False``
    reports without writing.
    """
    from .store import get_store

    store = store or get_store()
    cutoff_seconds = stale_run_cutoff(older_than_seconds)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=cutoff_seconds)

    stale = []
    for run in store.list_runs(status=RunStatus.running, limit=10_000):
        created = run.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < cutoff:
            stale.append(run)

    if apply:
        close_stale_runs(store, stale, minutes=cutoff_seconds // 60)
    if stale:
        logger.warning("%s %d stale run(s) that were still marked running",
                       "Reaped" if apply else "Found", len(stale))
    return stale


def prune_asset_cache(store=None, *, older_than_days: int | None = None,
                      apply: bool = True) -> dict:
    """Free disk by dropping cached assets that blob storage already holds.

    The media of RECENT runs is spared regardless of age: those are the ones a
    preview, a thumbnail or a republish is most likely to want, and fetching
    them back from blob for every page view is a poor trade for a few MB.

    ``ASSET_RETENTION_DAYS=0`` turns the prune OFF rather than meaning "delete
    everything" — an operator zeroing a retention setting is switching it off,
    and reading it the other way would wipe the cache. Deleting everything is
    still reachable, deliberately, via an explicit ``--older-than 0``.
    """
    from . import assets
    from .store import get_store

    days = settings.asset_retention_days if older_than_days is None else older_than_days
    if older_than_days is None and days <= 0:
        return {"applied": False, "deleted": 0, "freed_bytes": 0, "kept_local_only": 0,
                "skipped_recent": 0, "files": [],
                "skipped": "ASSET_RETENTION_DAYS=0 — the local cache is never pruned."}

    store = store or get_store()
    keep: set[str] = set()
    try:
        for run in store.list_runs(limit=200):
            for path in ([run.asset_path] + list(run.asset_paths or [])):
                if path:
                    keep.add(path.rsplit("/", 1)[-1])
    except Exception as exc:  # noqa: BLE001 - housekeeping must not fail a boot
        logger.warning("Could not list recent runs to spare their media: %s", exc)

    return assets.prune_local(days, apply=apply, keep=keep)


def close_stale_runs(store, runs, *, minutes: int = 0) -> int:
    """Mark the given abandoned runs failed, saying why. Returns how many."""
    for run in runs:
        run.status = RunStatus.failed
        how_long = f" after {minutes} minutes" if minutes else ""
        run.error = ((run.error or "") + (
            f"\nAbandoned: this run was still marked running{how_long}, which means "
            f"the service stopped (restart, deploy or crash) before it could finish. "
            f"Nothing was published by it — use 'Publish this again' if it had already "
            f"produced media, or re-run the agent.")).strip()
        store.update_run(run)
    return len(runs)


def retry_run(run_id: str, prompt: str = "") -> dict:
    """Run an earlier run's (instruction, account) pair again.

    Produces a NEW run rather than mutating the old one: the failed attempt is
    evidence and stays readable. ``prompt`` replaces the kickoff verbatim, which
    is how the dashboard lets an operator fix a bad brief — or drop a stale
    memory position out of it — and try again without editing the instruction
    itself. Empty ``prompt`` recomposes the kickoff from the instruction as usual.
    """
    store = get_store()
    original = store.get_run(run_id)
    if not original:
        return {"error": "not_found"}
    instruction = store.get_instruction(original.instruction_id)
    account = store.get_account(original.account_id)
    if not instruction:
        return {"error": "instruction_missing",
                "message": "The instruction this run belonged to has been deleted."}
    if not account:
        return {"error": "account_missing",
                "message": "The account this run targeted has been disconnected."}

    logger.info("Retrying run %s (instruction='%s', %s prompt)", run_id[:8], instruction.name,
                "edited" if prompt.strip() else "recomposed")
    return _run_one(instruction, account, store, prompt_override=prompt)


def _run_one(instruction: Instruction, account: Account, store,
             prompt_override: str = "") -> dict:
    # A rate-limited account cannot publish, so don't spend a run researching,
    # downloading and generating media that would be refused at the last step.
    if instruction.publish_mode is PublishMode.live and cooldown.is_active(account):
        waiting = cooldown.describe(account)
        logger.warning("Skipping %s / %s — publishing is rate-limited for another %s",
                       instruction.name, account.handle or account.external_id, waiting)
        return {"account_id": account.id, "status": "skipped",
                "reason": "rate_limited", "retry_in": waiting}

    lock_key = f"instr:{instruction.id}:acct:{account.id}"
    if not store.acquire_lock(lock_key, ttl_seconds=_LOCK_TTL):
        logger.info("Locked (already running): %s / %s", instruction.name, account.handle)
        return {"account_id": account.id, "status": "skipped", "reason": "locked"}

    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            workspace_id=instruction.workspace_id,
                            status=RunStatus.running))
    started = time.monotonic()
    # Tag every log line this run produces. Concurrent runs (a dashboard "Run now"
    # next to a scheduled fire) otherwise interleave indistinguishably.
    log_token = logging_setup.current_run_id.set(run.id[:8])
    try:
        logger.info("RUN START %s | instruction='%s' account=%s (%s) mode=%s media_pref=%s",
                    run.id[:8], instruction.name, account.handle or account.external_id,
                    account.platform.value, instruction.publish_mode.value,
                    instruction.media_pref.value)
        with _LockHeartbeat(store, lock_key):
            result = _run_async(_with_timeout(
                run_for_account(account, instruction, store, run, prompt_override)))
        result["account_id"] = account.id
        logger.info("RUN DONE  %s | %.1fs | %s",
                    run.id[:8], time.monotonic() - started, result)
        return result
    except TimeoutError:
        message = (f"Run exceeded {RUN_TIMEOUT_SECONDS}s and was abandoned. Something it "
                   f"called never returned — check the last log line before this one for "
                   f"where it stopped.")
        logger.error("RUN TIMEOUT %s | %.1fs | instruction='%s' account=%s",
                     run.id[:8], time.monotonic() - started, instruction.name,
                     account.handle or account.external_id)
        run.status = RunStatus.failed
        run.error = message
        store.update_run(run)
        return {"account_id": account.id, "status": "failed", "error": message}
    except Exception as exc:  # noqa: BLE001
        logger.exception("RUN FAILED %s | %.1fs | instruction='%s' account=%s",
                         run.id[:8], time.monotonic() - started, instruction.name,
                         account.handle or account.external_id)
        run.status = RunStatus.failed
        run.error = str(exc)
        store.update_run(run)
        return {"account_id": account.id, "status": "failed", "error": str(exc)}
    finally:
        store.release_lock(lock_key)
        logging_setup.current_run_id.reset(log_token)


def approve_staged(staged_id: str) -> dict:
    """Approve a pending post and publish it live (dashboard Approve action)."""
    store = get_store()
    staged = store.get_staged(staged_id)
    if not staged:
        return {"error": "not_found"}
    if staged.status != StagedStatus.pending_approval:
        return {"error": "not_pending", "status": staged.status.value}

    account = store.get_account(staged.account_id)
    if not account:
        return {"error": "account_missing"}
    platform = get_platform(account.platform)
    access_token = tokens.valid_access_token_sync(account, store)
    if not access_token:
        return {"error": "no_token", "message": "Reconnect the account in the dashboard."}

    # A staged REPLY is answered, not published: it sends through the platform's
    # reply path and records the engagement ledger, not the publish ledger. The
    # rest of this function is the post-publishing pipeline and does not apply.
    if staged.action_type == "reply":
        return _approve_staged_reply(store, staged, account, platform, access_token)

    kind = staged.media_kind or kind_from_path(staged.asset_path)
    # A staged carousel has several items and a story has a placement; passing only
    # asset_path posted the first image of a carousel as a lone feed post.
    paths = staged.asset_paths or ([staged.asset_path] if staged.asset_path else [])
    placement = staged.placement or "feed"

    digests = publish_ledger.fingerprints(paths, placement)
    # Same reality check as the agent's path: a post the human deleted by hand
    # must not block its content from ever being published again.
    duplicate = _run_async(_confirm_duplicate(account, store, platform, digests))
    if duplicate:
        index, already, confirmed = duplicate
        logger.warning("Refusing to approve a duplicate of an already-published post "
                       "(item %d/%d, %s, still-live=%s)", index + 1, len(digests),
                       already.get("url") or already.get("at"),
                       "yes" if confirmed else "unverified")
        which = f"Item {index + 1} of this post" if len(digests) > 1 else "This exact media"
        # A human is watching this one and can just click again, so the approval
        # path stays strict even when the check was inconclusive.
        state = ("is still live on the account" if confirmed
                 else "looks already published, though that could not be confirmed just now")
        return {"error": "already_published",
                "message": (f"{which} {state} — posted "
                            f"{publish_ledger.describe_entry(already)}. Not posting it twice. "
                            f"Reject this staged post, or try again in a few minutes if you "
                            f"believe the earlier one was deleted.")}

    try:
        result = _run_async(platform.publish(
            access_token=access_token, account=account,
            caption=staged.caption, asset_path=staged.asset_path, media_kind=kind,
            asset_paths=paths, placement=placement))
    except RateLimited as exc:
        held = cooldown.start(account, store, exc.retry_after_seconds,
                              reason="approval publish was rate-limited")
        logger.error("Approval publish rate-limited: %s", exc)
        return {"error": "rate_limited",
                "message": (f"{exc} — paused for {held // 60} minutes. The post is still "
                            f"pending; approve it again after that.")}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Approval publish failed")
        return {"error": "publish_failed", "message": str(exc)}

    publish_ledger.record(account, store, digests, url=result.url,
                          external_id=result.external_id,
                          instruction_id=staged.instruction_id)
    staged.status = StagedStatus.published
    staged.external_url = result.url
    store.update_staged(staged)
    _record_published_run(store, staged, result.url)
    return {"status": "published", "url": result.url}


def _approve_staged_reply(store, staged: StagedPost, account: Account, platform,
                          access_token: str) -> dict:
    """Send a staged reply now (the Approve button for an engagement reply).

    The reply's own duplicate guard (the engagement ledger) is re-checked here:
    the same target could have been answered by a live run between staging and
    approval. On success the ledger is recorded so no later run answers it again.
    """
    if engagement_ledger.answered(account, staged.target_type, staged.target_id):
        staged.status = StagedStatus.rejected
        store.update_staged(staged)
        return {"error": "already_answered",
                "message": "That comment has since been answered — nothing sent."}
    try:
        result = _run_async(platform.reply_to_target(
            access_token, account, target_type=staged.target_type,
            target_id=staged.target_id, text=staged.caption))
    except RateLimited as exc:
        held = cooldown.start(account, store, exc.retry_after_seconds,
                              reason="approval reply was rate-limited")
        logger.error("Approval reply rate-limited: %s", exc)
        return {"error": "rate_limited",
                "message": (f"{exc} — paused for {held // 60} minutes. The reply is still "
                            f"pending; approve it again after that.")}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Approval reply failed")
        return {"error": "reply_failed", "message": str(exc)}

    url = result.get("url", "") if isinstance(result, dict) else ""
    engagement_ledger.record(account, store, staged.target_type, staged.target_id,
                             url=url, instruction_id=staged.instruction_id)
    staged.status = StagedStatus.published
    staged.external_url = url
    store.update_staged(staged)
    run = store.get_run(staged.run_id) if staged.run_id else None
    if run:
        run.status = RunStatus.published
        run.log = (run.log + f"\nApproved & sent reply to {staged.target_type} "
                   f"{staged.target_id}" + (f": {url}" if url else "")).strip()
        store.update_run(run)
    return {"status": "replied", "url": url}


def reject_staged(staged_id: str) -> dict:
    store = get_store()
    staged = store.get_staged(staged_id)
    if not staged:
        return {"error": "not_found"}
    staged.status = StagedStatus.rejected
    store.update_staged(staged)
    return {"status": "rejected"}


def _record_published_run(store, staged: StagedPost, url: str) -> None:
    """Attach the published permalink to the originating run (best-effort).

    Fetches the run by id: scanning the most recent N runs used to miss anything
    approved after enough newer runs had piled up.
    """
    run = store.get_run(staged.run_id) if staged.run_id else None
    if not run:
        logger.warning("Approved post %s has no matching run (%s)", staged.id, staged.run_id)
        return
    run.status = RunStatus.published
    run.external_url = url
    run.log = (run.log + f"\nApproved & published: {url}").strip()
    store.update_run(run)
