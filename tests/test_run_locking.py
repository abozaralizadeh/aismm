"""Single-flight locks, heartbeats, and runs that never return.

The symptom: after using the dashboard's "Run now", scheduled runs stopped
happening — and stayed stopped after the manual run had finished.

Two mechanisms, both of which outlive the run that caused them:

* the manual run happens in a plain daemon thread, not a scheduler job. A
  gunicorn restart (``Restart=always``) kills that thread WITHOUT unwinding its
  ``finally``, so the lock it held was never released — and with a 30-minute TTL
  and no heartbeat, every scheduled run of that instruction was refused as
  "already running" by a run that no longer existed;
* APScheduler runs jobs with ``max_instances=1`` in a bounded thread pool, so a
  run that never returns silences its instruction forever and leaks a pool
  thread.
"""
import asyncio
import threading
import time

import pytest

from aismm import orchestrator
from aismm.models import Account, Instruction, PlatformName, PublishMode


@pytest.fixture()
def pair(store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="clinic", external_id="1"),
        access_token="t")
    instruction = store.upsert_instruction(
        Instruction(name="Comicbook", publish_mode=PublishMode.dry_run,
                    account_ids_json=f'["{account.id}"]'))
    return instruction, account


# --- the lock primitive ------------------------------------------------------------- #

def test_a_held_lock_blocks_a_second_acquire(store):
    assert store.acquire_lock("k", ttl_seconds=300) is True
    assert store.acquire_lock("k", ttl_seconds=300) is False


def test_releasing_frees_it(store):
    store.acquire_lock("k", ttl_seconds=300)
    store.release_lock("k")
    assert store.acquire_lock("k", ttl_seconds=300) is True


def test_a_stale_lock_is_reclaimed(store):
    """An orphaned lock — its owner died — must not block forever."""
    store.acquire_lock("k", ttl_seconds=300)
    assert store.acquire_lock("k", ttl_seconds=0) is True


def test_touch_keeps_a_lock_from_going_stale(store):
    store.acquire_lock("k", ttl_seconds=300)
    time.sleep(0.05)
    assert store.touch_lock("k") is True
    # Still held: a fresh lock is not reclaimable.
    assert store.acquire_lock("k", ttl_seconds=300) is False


def test_touching_a_lock_nobody_holds_reports_it(store):
    assert store.touch_lock("never-acquired") is False


def test_without_a_touch_an_aged_lock_goes_stale(store):
    """The baseline for the test below — otherwise it proves nothing."""
    store.acquire_lock("k", ttl_seconds=300)
    time.sleep(0.2)
    assert store.acquire_lock("k", ttl_seconds=0.1) is True     # aged out, reclaimed


def test_a_touch_resets_the_clock(store):
    """Same timings as above, but heartbeated — so it is still held."""
    store.acquire_lock("k", ttl_seconds=300)
    time.sleep(0.2)
    assert store.touch_lock("k") is True
    assert store.acquire_lock("k", ttl_seconds=0.1) is False    # fresh again


# --- the heartbeat ------------------------------------------------------------------ #

def test_the_heartbeat_refreshes_while_a_run_is_alive(store):
    store.acquire_lock("k", ttl_seconds=300)
    beats = []
    real_touch = store.touch_lock

    def counting_touch(key):
        beats.append(key)
        return real_touch(key)

    store.touch_lock = counting_touch
    with orchestrator._LockHeartbeat(store, "k", interval=0.05):
        time.sleep(0.3)
    assert len(beats) >= 3


def test_the_heartbeat_stops_when_the_run_ends(store):
    store.acquire_lock("k", ttl_seconds=300)
    beats = []
    store.touch_lock = lambda key: (beats.append(key), True)[1]

    with orchestrator._LockHeartbeat(store, "k", interval=0.05):
        time.sleep(0.2)
    settled = len(beats)
    time.sleep(0.3)
    assert len(beats) == settled, "the heartbeat outlived its run"


def test_the_heartbeat_thread_is_a_daemon(store):
    """It must never hold the process open on its own."""
    with orchestrator._LockHeartbeat(store, "k", interval=5) as beat:
        assert beat._thread.daemon is True


def test_a_failing_touch_does_not_kill_the_run(store):
    def explode(key):
        raise RuntimeError("table storage is having a moment")

    store.touch_lock = explode
    with orchestrator._LockHeartbeat(store, "k", interval=0.05):
        time.sleep(0.15)      # survives; the error is logged, not raised


# --- a run that never returns -------------------------------------------------------- #

def test_a_wedged_run_is_abandoned_not_left_hanging(store, monkeypatch, pair):
    """The whole point: the pool thread comes back, and so does the instruction."""
    instruction, account = pair
    monkeypatch.setattr(orchestrator, "RUN_TIMEOUT_SECONDS", 0.2)

    async def never_returns(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(orchestrator, "run_for_account", never_returns)

    started = time.monotonic()
    result = orchestrator._run_one(instruction, account, store)
    assert time.monotonic() - started < 5
    assert result["status"] == "failed"
    assert "exceeded" in result["error"]


def test_a_wedged_run_still_releases_its_lock(store, monkeypatch, pair):
    instruction, account = pair
    monkeypatch.setattr(orchestrator, "RUN_TIMEOUT_SECONDS", 0.2)

    async def never_returns(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(orchestrator, "run_for_account", never_returns)
    orchestrator._run_one(instruction, account, store)

    lock_key = f"instr:{instruction.id}:acct:{account.id}"
    assert store.acquire_lock(lock_key, ttl_seconds=300) is True


def test_a_timed_out_run_is_recorded_as_failed(store, monkeypatch, pair):
    instruction, account = pair
    monkeypatch.setattr(orchestrator, "RUN_TIMEOUT_SECONDS", 0.2)

    async def never_returns(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(orchestrator, "run_for_account", never_returns)
    orchestrator._run_one(instruction, account, store)

    runs = store.list_runs(limit=5, instruction_id=instruction.id)
    assert runs and runs[0].status.value == "failed"
    assert "never returned" in runs[0].error


def test_a_crashing_run_releases_its_lock_too(store, monkeypatch, pair):
    instruction, account = pair

    async def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "run_for_account", explode)
    result = orchestrator._run_one(instruction, account, store)
    assert result["status"] == "failed"
    lock_key = f"instr:{instruction.id}:acct:{account.id}"
    assert store.acquire_lock(lock_key, ttl_seconds=300) is True


# --- a manual run and a scheduled run overlapping ------------------------------------ #

def test_a_manual_run_does_not_block_a_different_instruction(store, monkeypatch):
    """Locks are per (instruction, account) — separate work must stay independent."""
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="a", external_id="1"),
        access_token="t")
    other = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="b", external_id="2"),
        access_token="t")
    one = store.upsert_instruction(Instruction(name="One", publish_mode=PublishMode.dry_run))
    two = store.upsert_instruction(Instruction(name="Two", publish_mode=PublishMode.dry_run))

    holding = threading.Event()

    async def slow(*args, **kwargs):
        holding.set()
        await asyncio.sleep(0.4)
        return {"mode": "dry_run"}

    async def quick(*args, **kwargs):
        return {"mode": "dry_run"}

    monkeypatch.setattr(orchestrator, "run_for_account", slow)
    manual = threading.Thread(target=orchestrator._run_one, args=(one, account, store))
    manual.start()
    holding.wait(2)

    monkeypatch.setattr(orchestrator, "run_for_account", quick)
    result = orchestrator._run_one(two, other, store)
    manual.join(5)
    assert result.get("status") != "skipped", "an unrelated instruction was blocked"


def test_the_same_pair_is_single_flighted(store, monkeypatch, pair):
    """The manual run holds it; the concurrent scheduled fire is skipped, not doubled."""
    instruction, account = pair
    holding = threading.Event()

    async def slow(*args, **kwargs):
        holding.set()
        await asyncio.sleep(0.4)
        return {"mode": "dry_run"}

    monkeypatch.setattr(orchestrator, "run_for_account", slow)
    manual = threading.Thread(target=orchestrator._run_one, args=(instruction, account, store))
    manual.start()
    holding.wait(2)

    result = orchestrator._run_one(instruction, account, store)
    manual.join(5)
    assert result["status"] == "skipped" and result["reason"] == "locked"


def test_and_the_pair_is_runnable_again_once_it_finishes(store, monkeypatch, pair):
    """The failure being fixed: it stayed blocked AFTER the run had finished."""
    instruction, account = pair

    async def quick(*args, **kwargs):
        return {"mode": "dry_run"}

    monkeypatch.setattr(orchestrator, "run_for_account", quick)
    orchestrator._run_one(instruction, account, store)
    second = orchestrator._run_one(instruction, account, store)
    assert second.get("status") != "skipped"


def test_an_orphaned_lock_clears_within_the_ttl(store, pair):
    """Simulates the killed 'Run now' thread: lock left behind, nobody heartbeating."""
    instruction, account = pair
    lock_key = f"instr:{instruction.id}:acct:{account.id}"
    assert store.acquire_lock(lock_key, ttl_seconds=orchestrator._LOCK_TTL)

    # Nobody renews it. Once a TTL passes it is reclaimable — 5 minutes, not 30.
    assert orchestrator._LOCK_TTL <= 600
    assert store.acquire_lock(lock_key, ttl_seconds=0) is True
