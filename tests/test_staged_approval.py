"""Approval-queue behaviour: reject reflects on the run, and approve can schedule.

Three operator-reported gaps are pinned here:
  1. Rejecting a queued post must move the RUN off "staged" to "rejected" (it used
     to update only the StagedPost, so the run sat on "staged" forever).
  2. Approve may schedule a post for a specific time instead of publishing now.
  3. The scheduled sweep publishes due posts and leaves not-yet-due ones alone.
"""
import datetime as dt

from aismm.models import (
    Account, Instruction, PlatformName, Run, RunStatus, StagedPost, StagedStatus,
)
from aismm.platforms.base import Capabilities, PublishResult
from aismm import orchestrator


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _staged_post(store, *, status=StagedStatus.pending_approval, publish_at=None,
                 action_type="post", run_status=RunStatus.staged, monkeypatch=None):
    if monkeypatch is not None:
        # approve_staged / reject_staged reach for get_store() internally.
        monkeypatch.setattr(orchestrator, "get_store", lambda: store)
    acct = store.upsert_account(Account(platform=PlatformName.twitter, handle="bot"),
                                access_token="tok")
    instr = store.upsert_instruction(Instruction(name="i"))
    run = store.add_run(Run(instruction_id=instr.id, account_id=acct.id, status=run_status))
    staged = store.add_staged(StagedPost(
        instruction_id=instr.id, account_id=acct.id, run_id=run.id,
        caption="hello", media_kind="text", action_type=action_type,
        target_type="comment" if action_type == "reply" else "",
        target_id="c1" if action_type == "reply" else "",
        status=status, publish_at=publish_at))
    return acct, instr, run, staged


def _fake_publish(monkeypatch):
    """Stub the platform + duplicate guard so a publish is deterministic, no network."""
    sent = {}

    class FakePlatform:
        capabilities = Capabilities(True, True, True, False, "landscape", 280)

        async def publish(self, **kwargs):
            sent["caption"] = kwargs.get("caption")
            return PublishResult(url="https://x.com/bot/status/1", external_id="1")

    async def _no_duplicate(*a, **k):
        return None

    monkeypatch.setattr(orchestrator, "get_platform", lambda name: FakePlatform())
    monkeypatch.setattr(orchestrator, "_confirm_duplicate", _no_duplicate)
    monkeypatch.setattr(orchestrator.tokens, "valid_access_token_sync",
                        lambda account, store: "tok")
    return sent


# --- reject reflects on the run ------------------------------------------------------ #

def test_reject_marks_the_post_and_the_run_rejected(store, monkeypatch):
    _acct, _instr, run, staged = _staged_post(store, monkeypatch=monkeypatch)
    res = orchestrator.reject_staged(staged.id)
    assert res["status"] == "rejected"
    assert store.get_staged(staged.id).status is StagedStatus.rejected
    assert store.get_run(run.id).status is RunStatus.rejected


def test_reject_does_not_touch_the_run_for_a_reply(store, monkeypatch):
    """An engage run stages one reply PER comment; rejecting one must not flip the
    whole run, whose status is governed by the run-wide engagement tally."""
    _acct, _instr, run, staged = _staged_post(store, action_type="reply",
                                              monkeypatch=monkeypatch)
    orchestrator.reject_staged(staged.id)
    assert store.get_staged(staged.id).status is StagedStatus.rejected
    assert store.get_run(run.id).status is RunStatus.staged      # unchanged


# --- approve can schedule for later -------------------------------------------------- #

def test_approve_with_a_future_time_schedules_and_does_not_publish(store, monkeypatch):
    sent = _fake_publish(monkeypatch)
    _acct, _instr, run, staged = _staged_post(store, monkeypatch=monkeypatch)
    when = _now() + dt.timedelta(hours=3)

    res = orchestrator.approve_staged(staged.id, publish_at=when)

    assert res["status"] == "scheduled"
    fresh = store.get_staged(staged.id)
    assert fresh.status is StagedStatus.approved
    assert fresh.publish_at is not None
    assert "caption" not in sent                       # nothing was published
    assert store.get_run(run.id).status is RunStatus.staged


def test_approve_with_a_past_time_publishes_now(store, monkeypatch):
    """A time already in the past is not a schedule — publish immediately."""
    sent = _fake_publish(monkeypatch)
    _acct, _instr, _run, staged = _staged_post(store, monkeypatch=monkeypatch)

    res = orchestrator.approve_staged(staged.id, publish_at=_now() - dt.timedelta(minutes=1))

    assert res["status"] == "published"
    assert sent["caption"] == "hello"
    assert store.get_staged(staged.id).status is StagedStatus.published


def test_reject_cancels_a_scheduled_post(store, monkeypatch):
    _acct, _instr, run, staged = _staged_post(
        store, status=StagedStatus.approved, publish_at=_now() + dt.timedelta(days=1),
        monkeypatch=monkeypatch)
    orchestrator.reject_staged(staged.id)
    fresh = store.get_staged(staged.id)
    assert fresh.status is StagedStatus.rejected
    assert fresh.publish_at is None
    assert store.get_run(run.id).status is RunStatus.rejected


# --- the due-publish sweep ----------------------------------------------------------- #

def test_list_due_staged_only_returns_approved_posts_whose_time_has_come(store):
    _staged_post(store, status=StagedStatus.approved, publish_at=_now() - dt.timedelta(minutes=5))
    _staged_post(store, status=StagedStatus.approved, publish_at=_now() + dt.timedelta(hours=1))
    _staged_post(store, status=StagedStatus.pending_approval)      # not scheduled at all
    due = store.list_due_staged(_now())
    assert len(due) == 1
    assert due[0].status is StagedStatus.approved


def test_publish_due_staged_publishes_the_due_ones(store, monkeypatch):
    sent = _fake_publish(monkeypatch)
    _a, _i, _r, due = _staged_post(
        store, status=StagedStatus.approved, publish_at=_now() - dt.timedelta(minutes=5))
    _staged_post(store, status=StagedStatus.approved,
                 publish_at=_now() + dt.timedelta(hours=1))       # future — left alone

    results = orchestrator.publish_due_staged(store)

    assert sent["caption"] == "hello"
    assert len(results) == 1 and results[0]["status"] == "published"
    assert store.get_staged(due.id).status is StagedStatus.published


def test_a_doomed_scheduled_post_returns_to_the_queue(store, monkeypatch):
    """A hard failure (bad token) must not retry every minute forever — it goes
    back to pending_approval for a human, not stuck approved."""
    _a, _i, _r, due = _staged_post(
        store, status=StagedStatus.approved, publish_at=_now() - dt.timedelta(minutes=5))
    monkeypatch.setattr(orchestrator.tokens, "valid_access_token_sync",
                        lambda account, store: "")            # no token → hard failure

    orchestrator.publish_due_staged(store)

    fresh = store.get_staged(due.id)
    assert fresh.status is StagedStatus.pending_approval
    assert fresh.publish_at is None
