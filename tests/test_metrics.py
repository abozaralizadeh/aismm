"""The performance feedback loop: poll counters, feed them back, show them.

Covers the pure display/format helpers and the two orchestrator sweeps
(``refresh_metrics`` daily; ``refresh_run_metrics`` the dashboard button). The
per-platform ``fetch_post_metrics`` signatures are pinned in
``test_store_interface.py`` (the drift guard) — here we stub a platform so the
sweep logic is tested without network or credentials.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aismm import orchestrator, tokens
from aismm.agent.prompts import build_performance_block
from aismm.dashboard.metrics_display import format_metrics
from aismm.models import Account, PlatformName, Run, RunStatus
from aismm.platforms.base import SocialPlatform
from aismm.tools.performance_tool import recent_performance_runs


# --- display helper (dashboard pills) ------------------------------------------------ #

def test_format_metrics_orders_labels_and_formats_values():
    pills = format_metrics({"likes": 85, "views": 1200, "upvote_ratio": 0.95})
    # views (reach-ish) before likes; ratio rendered as a percent; ints get commas.
    assert pills == [
        {"label": "views", "value": "1,200"},
        {"label": "likes", "value": "85"},
        {"label": "upvote ratio", "value": "95%"},
    ]


def test_format_metrics_drops_bools_and_keeps_unknown_keys():
    pills = format_metrics({"liked": True, "brand_new_metric": 7})
    assert pills == [{"label": "brand new metric", "value": "7"}]


def test_format_metrics_empty():
    assert format_metrics({}) == []
    assert format_metrics(None) == []


# --- kickoff performance block (agent-facing) ---------------------------------------- #

def _run_with(metrics, *, caption="Panel 7", when=None):
    run = Run(instruction_id="i", account_id="a", platform=PlatformName.twitter,
              status=RunStatus.published, caption=caption,
              created_at=when or datetime(2026, 8, 1, tzinfo=timezone.utc))
    run.set_metrics(metrics)
    return run


def test_build_performance_block_renders_only_runs_with_metrics():
    runs = [_run_with({"likes": 10, "impressions": 500}),
            _run_with({}, caption="not polled yet")]
    block = build_performance_block(runs)
    assert "RECENT PERFORMANCE" in block
    assert "10 likes" in block and "500 impressions" in block
    assert "not polled yet" not in block


def test_build_performance_block_empty_when_nothing_has_metrics():
    assert build_performance_block([_run_with({})]) == ""
    assert build_performance_block([]) == ""


# --- recent_performance_runs (the pull half) ----------------------------------------- #

def test_recent_performance_runs_returns_only_polled_published_runs(store):
    acct = store.upsert_account(
        Account(platform=PlatformName.twitter, handle="h", external_id="x"),
        access_token="t")
    polled = store.add_run(Run(instruction_id="i", account_id=acct.id,
                               platform=PlatformName.twitter, status=RunStatus.published,
                               external_id="p1"))
    polled.set_metrics({"likes": 3})
    store.update_run(polled)
    # Published but never polled — excluded (no metrics yet).
    store.add_run(Run(instruction_id="i", account_id=acct.id, platform=PlatformName.twitter,
                      status=RunStatus.published, external_id="p2"))
    got = recent_performance_runs(store, acct.id)
    assert [r.external_id for r in got] == ["p1"]


def test_recent_performance_tool_is_registered():
    from aismm.tools.registry import registered_tool_names

    assert "recent_performance" in registered_tool_names()


# --- refresh_metrics (the daily sweep) ----------------------------------------------- #

class _Caps:
    def __init__(self, supports_metrics=True):
        self.supports_metrics = supports_metrics


class _FakePlatform:
    """Stand-in platform: records every external_id it was asked about.

    It borrows the REAL ``fetch_post_metrics_bulk`` default rather than stubbing
    one, so these tests exercise the fallback a platform without a batch endpoint
    actually takes — the per-post loop.
    """

    fetch_post_metrics_bulk = SocialPlatform.fetch_post_metrics_bulk

    def __init__(self, metrics, *, supports_metrics=True):
        self._metrics = metrics
        self.capabilities = _Caps(supports_metrics)
        self.polled: list[str] = []

    async def fetch_post_metrics(self, access_token, account, *, external_id):
        self.polled.append(external_id)
        return self._metrics


class _BulkPlatform(_FakePlatform):
    """A platform that answers many ids in ONE call, like X's GET /2/tweets."""

    def __init__(self, metrics, **kw):
        super().__init__(metrics, **kw)
        self.calls: list[list[str]] = []

    async def fetch_post_metrics_bulk(self, access_token, account, *, external_ids):
        self.calls.append(list(external_ids))
        return {external_id: dict(self._metrics) for external_id in external_ids}


@pytest.fixture()
def wired(store, monkeypatch):
    """A published, pollable run plus token/platform stubs the sweep uses."""
    monkeypatch.setattr(orchestrator, "get_store", lambda: store)
    monkeypatch.setattr(tokens, "valid_access_token_sync", lambda account, s: "tok")
    acct = store.upsert_account(
        Account(platform=PlatformName.twitter, handle="h", external_id="x"),
        access_token="t")
    run = store.add_run(Run(instruction_id="i", account_id=acct.id,
                            platform=PlatformName.twitter, status=RunStatus.published,
                            external_id="tw1"))
    return store, acct, run


def test_refresh_metrics_writes_counters(wired, monkeypatch):
    store, _acct, run = wired
    platform = _FakePlatform({"likes": 42, "impressions": 900})
    monkeypatch.setattr(orchestrator, "get_platform", lambda _p: platform)
    summary = orchestrator.refresh_metrics(store, max_age_days=30)
    assert summary["updated"] == 1 and summary["polled"] == 1
    assert store.get_run(run.id).metrics == {"likes": 42, "impressions": 900}
    assert store.get_run(run.id).metrics_updated_at is not None


def test_refresh_metrics_dry_run_polls_but_does_not_write(wired, monkeypatch):
    store, _acct, run = wired
    platform = _FakePlatform({"likes": 42})
    monkeypatch.setattr(orchestrator, "get_platform", lambda _p: platform)
    summary = orchestrator.refresh_metrics(store, max_age_days=30, apply=False)
    assert summary["applied"] is False and summary["polled"] == 1
    assert store.get_run(run.id).metrics == {}       # untouched


def test_refresh_metrics_skips_platforms_without_a_metrics_api(wired, monkeypatch):
    store, _acct, run = wired
    platform = _FakePlatform({"likes": 1}, supports_metrics=False)
    monkeypatch.setattr(orchestrator, "get_platform", lambda _p: platform)
    summary = orchestrator.refresh_metrics(store, max_age_days=30)
    assert summary["skipped"] == 1 and summary["polled"] == 0 and platform.polled == []


def test_refresh_metrics_none_result_leaves_last_values_alone(wired, monkeypatch):
    store, _acct, run = wired
    run.set_metrics({"likes": 5})               # a previous, good poll
    store.update_run(run)
    platform = _FakePlatform(None)              # this poll could not read the post
    monkeypatch.setattr(orchestrator, "get_platform", lambda _p: platform)
    summary = orchestrator.refresh_metrics(store, max_age_days=30)
    assert summary["updated"] == 0 and platform.polled == ["tw1"]
    assert store.get_run(run.id).metrics == {"likes": 5}     # not clobbered


def test_one_accounts_posts_are_polled_in_a_single_call(wired, monkeypatch):
    """The sweep asks ONCE per account, not once per post.

    X bills per request and takes 100 ids on one lookup, so a month of daily
    posting was costing a request per post per morning for an answer that fits in
    one. Grouping is what makes the batch endpoint reachable at all.
    """
    store, acct, _run = wired
    for external_id in ("tw2", "tw3"):
        store.add_run(Run(instruction_id="i", account_id=acct.id,
                          platform=PlatformName.twitter, status=RunStatus.published,
                          external_id=external_id))
    platform = _BulkPlatform({"likes": 7})
    monkeypatch.setattr(orchestrator, "get_platform", lambda _p: platform)
    summary = orchestrator.refresh_metrics(store, max_age_days=30)
    assert summary["updated"] == 3
    assert len(platform.calls) == 1 and sorted(platform.calls[0]) == ["tw1", "tw2", "tw3"]


def test_a_post_whose_counters_have_settled_is_not_re_polled_daily(wired, monkeypatch):
    """An old post polled recently is skipped; a fresh one is always due.

    Engagement arrives in a burst and then trickles. Re-reading a three-week-old
    post every morning for 30 days buys nothing and is charged for every time.
    """
    store, acct, fresh = wired
    old = store.add_run(Run(
        instruction_id="i", account_id=acct.id, platform=PlatformName.twitter,
        status=RunStatus.published, external_id="tw_old",
        created_at=datetime.now(timezone.utc) - timedelta(days=20)))
    old.metrics_updated_at = datetime.now(timezone.utc) - timedelta(hours=24)
    store.update_run(old)
    platform = _BulkPlatform({"likes": 1})
    monkeypatch.setattr(orchestrator, "get_platform", lambda _p: platform)
    summary = orchestrator.refresh_metrics(store, max_age_days=30)
    assert summary["settled"] == 1                       # the 20-day-old one waits a week
    assert platform.calls == [[fresh.external_id]]


def test_a_settled_post_comes_back_round_once_its_interval_passes(wired, monkeypatch):
    """Skipping is a CADENCE, not a retirement — the post is polled again later."""
    store, acct, _fresh = wired
    old = store.add_run(Run(
        instruction_id="i", account_id=acct.id, platform=PlatformName.twitter,
        status=RunStatus.published, external_id="tw_old",
        created_at=datetime.now(timezone.utc) - timedelta(days=20)))
    old.metrics_updated_at = datetime.now(timezone.utc) - timedelta(days=8)
    store.update_run(old)
    platform = _BulkPlatform({"likes": 1})
    monkeypatch.setattr(orchestrator, "get_platform", lambda _p: platform)
    summary = orchestrator.refresh_metrics(store, max_age_days=30)
    assert summary["settled"] == 0 and sorted(platform.calls[0]) == ["tw1", "tw_old"]


def test_a_post_that_has_never_been_polled_is_always_due(wired, monkeypatch):
    """The FIRST read is what fills the dashboard in, whatever the post's age."""
    store, acct, _fresh = wired
    store.add_run(Run(
        instruction_id="i", account_id=acct.id, platform=PlatformName.twitter,
        status=RunStatus.published, external_id="tw_ancient",
        created_at=datetime.now(timezone.utc) - timedelta(days=25)))
    platform = _BulkPlatform({"likes": 1})
    monkeypatch.setattr(orchestrator, "get_platform", lambda _p: platform)
    orchestrator.refresh_metrics(store, max_age_days=30)
    assert sorted(platform.calls[0]) == ["tw1", "tw_ancient"]


def test_refresh_metrics_off_when_days_zero(wired, monkeypatch):
    store, _acct, _run = wired
    monkeypatch.setattr(orchestrator, "get_platform",
                        lambda _p: (_ for _ in ()).throw(AssertionError("must not poll")))
    summary = orchestrator.refresh_metrics(store, max_age_days=0)
    assert summary["polled"] == 0 and "off" in summary["skipped_reason"]


# --- refresh_run_metrics (the dashboard button) -------------------------------------- #

def test_refresh_run_metrics_writes_one_run(wired, monkeypatch):
    store, _acct, run = wired
    platform = _FakePlatform({"likes": 7})
    monkeypatch.setattr(orchestrator, "get_platform", lambda _p: platform)
    metrics = orchestrator.refresh_run_metrics(run.id, store)
    assert metrics == {"likes": 7}
    assert store.get_run(run.id).metrics == {"likes": 7}
    assert platform.polled == ["tw1"]


def test_refresh_run_metrics_none_when_no_external_id(store, monkeypatch):
    monkeypatch.setattr(tokens, "valid_access_token_sync", lambda a, s: "tok")
    run = store.add_run(Run(instruction_id="i", account_id="a", platform=PlatformName.twitter,
                            status=RunStatus.published, external_id=""))
    assert orchestrator.refresh_run_metrics(run.id, store) is None


def test_refresh_run_metrics_none_when_platform_has_no_metrics_api(wired, monkeypatch):
    store, _acct, run = wired
    platform = _FakePlatform({"likes": 1}, supports_metrics=False)
    monkeypatch.setattr(orchestrator, "get_platform", lambda _p: platform)
    assert orchestrator.refresh_run_metrics(run.id, store) is None
    assert store.get_run(run.id).metrics == {}
