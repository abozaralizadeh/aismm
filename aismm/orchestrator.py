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

from . import cooldown, logging_setup, publish_ledger
from .agent import run_for_account
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
RUN_TIMEOUT_SECONDS = 3600


def _run_async(coro):
    """Run an async coroutine from a sync context (scheduler thread / CLI)."""
    return asyncio.run(coro)


async def _with_timeout(coro, seconds: int = 0):
    """Abandon a run that has stopped making progress.

    ``asyncio.TimeoutError`` is ``TimeoutError`` on 3.11+; normalized here so the
    caller can catch the builtin on every supported version.
    """
    try:
        return await asyncio.wait_for(coro, timeout=seconds or RUN_TIMEOUT_SECONDS)
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


def _run_one(instruction: Instruction, account: Account, store) -> dict:
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
                run_for_account(account, instruction, store, run)))
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
    access_token, _ = store.get_tokens(account.id)
    if not access_token:
        return {"error": "no_token", "message": "Reconnect the account in the dashboard."}

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
