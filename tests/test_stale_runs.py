"""Runs stuck on "running" when nothing is running them.

A run is only ever moved off ``running`` by the code executing it. The wall-clock
ceiling closes one that overruns — but only while the process is alive. A
gunicorn restart, a deploy or an OOM kill mid-run strands the row, and the Runs
page fills with work that will never finish.

The lock such a run held clears itself within one TTL (it is heartbeated), so the
*instruction* recovers on its own. The run row does not.
"""
import dataclasses
import datetime as dt

import pytest

from aismm import config as config_module
from aismm import orchestrator
from aismm.config import AuthSettings
from aismm.dashboard import app as app_module
from aismm.dashboard import sso
from aismm.models import Account, Instruction, PlatformName, Run, RunStatus

UTC = dt.timezone.utc


@pytest.fixture()
def setup(store, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_store", lambda: store, raising=False)
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="ig", external_id="1"),
        access_token="t")
    instruction = store.upsert_instruction(Instruction(name="Comicbook"))
    return instruction, account


def _run(store, instruction, account, *, age_seconds, status=RunStatus.running):
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            status=status))
    run.created_at = dt.datetime.now(UTC) - dt.timedelta(seconds=age_seconds)
    store.update_run(run)
    return run


# --- what counts as abandoned --------------------------------------------------------- #

def test_an_old_running_run_is_closed(store, setup):
    instruction, account = setup
    stranded = _run(store, instruction, account, age_seconds=orchestrator.stale_run_cutoff() + 60)
    assert len(orchestrator.reap_stale_runs(store)) == 1
    assert store.get_run(stranded.id).status is RunStatus.failed


def test_a_young_running_run_is_left_alone(store, setup):
    """It may well still be working — the ceiling has not passed."""
    instruction, account = setup
    live = _run(store, instruction, account, age_seconds=60)
    assert orchestrator.reap_stale_runs(store) == []
    assert store.get_run(live.id).status is RunStatus.running


def test_a_run_just_inside_the_cutoff_is_left_alone(store, setup):
    instruction, account = setup
    edge = _run(store, instruction, account, age_seconds=orchestrator.stale_run_cutoff() - 60)
    assert orchestrator.reap_stale_runs(store) == []
    assert store.get_run(edge.id).status is RunStatus.running


@pytest.mark.parametrize("status", [RunStatus.published, RunStatus.failed,
                                    RunStatus.staged, RunStatus.skipped])
def test_finished_runs_are_never_touched(store, setup, status):
    instruction, account = setup
    done = _run(store, instruction, account, age_seconds=999_999, status=status)
    orchestrator.reap_stale_runs(store)
    assert store.get_run(done.id).status is status


def test_the_cutoff_follows_the_run_ceiling(monkeypatch):
    """A live run cannot outlast RUN_TIMEOUT_SECONDS, so that plus a grace period
    is a safe line — anything past it has no process behind it."""
    monkeypatch.setattr(orchestrator, "RUN_TIMEOUT_SECONDS", 3600)
    assert orchestrator.stale_run_cutoff() == 3600 + orchestrator._REAP_GRACE_SECONDS


def test_with_the_ceiling_disabled_it_falls_back_to_a_day(monkeypatch):
    """RUN_TIMEOUT_SECONDS=0 leaves nothing to derive a bound from."""
    monkeypatch.setattr(orchestrator, "RUN_TIMEOUT_SECONDS", 0)
    assert orchestrator.stale_run_cutoff() == orchestrator._REAP_FALLBACK_SECONDS


def test_an_explicit_age_wins(monkeypatch):
    assert orchestrator.stale_run_cutoff(120) == 120


# --- it reports before it writes ------------------------------------------------------ #

def test_a_dry_run_changes_nothing(store, setup):
    instruction, account = setup
    stranded = _run(store, instruction, account, age_seconds=999_999)
    found = orchestrator.reap_stale_runs(store, apply=False)
    assert [r.id for r in found] == [stranded.id]
    assert store.get_run(stranded.id).status is RunStatus.running


def test_the_error_says_what_happened_and_what_to_do(store, setup):
    instruction, account = setup
    stranded = _run(store, instruction, account, age_seconds=999_999)
    orchestrator.reap_stale_runs(store)
    error = store.get_run(stranded.id).error
    assert "the service stopped" in error
    assert "Nothing was published by it" in error
    assert "Publish this again" in error


def test_an_existing_error_is_kept(store, setup):
    instruction, account = setup
    stranded = _run(store, instruction, account, age_seconds=999_999)
    stranded.error = "Sora timed out"
    store.update_run(stranded)
    orchestrator.reap_stale_runs(store)
    assert "Sora timed out" in store.get_run(stranded.id).error


# --- the startup sweep ---------------------------------------------------------------- #

def test_starting_the_scheduler_closes_them(store, setup, monkeypatch):
    """Booting is exactly when they exist, so a deploy tidies up after itself."""
    from aismm import scheduler

    import aismm.store as store_module

    instruction, account = setup
    stranded = _run(store, instruction, account, age_seconds=999_999)
    # reap_stale_runs() resolves the store itself, so patch it at the source.
    monkeypatch.setattr(store_module, "get_store", lambda: store)
    monkeypatch.setattr(scheduler, "refresh_jobs", lambda: None)
    monkeypatch.setattr(scheduler, "get_scheduler",
                        lambda: type("S", (), {"running": True, "start": lambda self: None})())
    scheduler.start()
    assert store.get_run(stranded.id).status is RunStatus.failed


def test_a_failure_while_reaping_does_not_stop_the_scheduler(monkeypatch):
    from aismm import scheduler

    monkeypatch.setattr(orchestrator, "reap_stale_runs",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    started = {}
    monkeypatch.setattr(scheduler, "refresh_jobs", lambda: started.setdefault("jobs", True))
    monkeypatch.setattr(scheduler, "get_scheduler",
                        lambda: type("S", (), {"running": True, "start": lambda self: None})())
    scheduler.start()
    assert started["jobs"] is True          # tidying must never block the scheduler


# --- through the dashboard ------------------------------------------------------------ #

@pytest.fixture()
def dash(store, monkeypatch, tmp_path):
    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


def test_the_runs_page_offers_to_close_them(dash, store, setup):
    instruction, account = setup
    _run(store, instruction, account, age_seconds=999_999)
    page = dash.test_client().get("/runs").get_data(as_text=True)
    assert "still marked" in page
    assert "Close them" in page


def test_the_offer_is_absent_when_there_is_nothing_to_close(dash, store, setup):
    instruction, account = setup
    _run(store, instruction, account, age_seconds=60)
    page = dash.test_client().get("/runs").get_data(as_text=True)
    assert "Close them" not in page


def test_the_button_closes_them(dash, store, setup):
    instruction, account = setup
    stranded = _run(store, instruction, account, age_seconds=999_999)
    page = dash.test_client().post("/runs/reap", follow_redirects=True).get_data(as_text=True)
    assert "Closed 1 abandoned run(s)" in page
    assert store.get_run(stranded.id).status is RunStatus.failed
