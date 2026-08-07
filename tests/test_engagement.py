"""The engagement gate: replies obey ``publish_mode`` and never answer twice.

``perform_reply`` is the reply-side mirror of ``perform_publish``. The two things
that must hold on EVERY path — because a cron "respond to comments" instruction
re-reads the same thread every time it fires — are (1) a reply obeys the
instruction's ``publish_mode`` (dry-run previews, approval queues, live sends) and
(2) a target already answered (or already staged) is never answered again. Both
live in code, not in the prompt, so these tests pin them without the network.
"""
import asyncio

import pytest

from aismm import engagement, engagement_ledger
from aismm.models import (
    Account, Instruction, InstructionTask, PlatformName, PublishMode, Run,
    StagedPost, StagedStatus,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(store):
    """An Instagram account (supports_comments) + a run + a state dict."""
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="me", external_id="ig1"),
        access_token="tok")
    instruction = store.upsert_instruction(
        Instruction(name="Engage", task_type=InstructionTask.engage,
                    publish_mode=PublishMode.dry_run,
                    account_ids_json=f'["{account.id}"]'))
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id))
    state = {"account": account, "instruction": instruction, "store": store,
             "run": run, "assets": []}
    return state


def _reply(state, **kw):
    kw.setdefault("target_type", "comment")
    kw.setdefault("target_id", "c1")
    kw.setdefault("text", "thanks for reading!")
    return _run(engagement.perform_reply(state, **kw))


# --- the mode gate ------------------------------------------------------------------- #

def test_dry_run_stages_a_preview_and_calls_no_platform(env, monkeypatch):
    def _boom(*a, **k):  # a dry run must never reach the platform
        raise AssertionError("dry_run must not call the platform")
    monkeypatch.setattr("aismm.tokens.valid_access_token", _boom)

    result = _reply(env, target_excerpt="Loved this!")
    assert result["status"] == "staged" and result["mode"] == "dry_run"

    staged = env["store"].list_staged(pending_only=False)
    assert len(staged) == 1
    s = staged[0]
    assert s.action_type == "reply"
    assert s.target_type == "comment" and s.target_id == "c1"
    assert s.target_excerpt == "Loved this!"
    assert s.caption == "thanks for reading!"
    assert s.status is StagedStatus.preview
    assert env["run"].id == s.run_id
    assert env["engagement"]["staged"] == 1


def test_approval_queues_for_a_human_and_calls_no_platform(env, monkeypatch):
    monkeypatch.setattr("aismm.tokens.valid_access_token",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API in approval")))
    env["instruction"].publish_mode = PublishMode.approval

    result = _reply(env)
    assert result["status"] == "pending_approval"
    staged = env["store"].list_staged(pending_only=True)
    assert len(staged) == 1 and staged[0].status is StagedStatus.pending_approval
    assert staged[0].action_type == "reply"


def test_live_sends_and_records_the_ledger(env, monkeypatch):
    sent = {}

    async def _fake_reply(access_token, account, *, target_type, target_id, text):
        sent.update(target_type=target_type, target_id=target_id, text=text)
        return {"id": "r99", "url": "https://example.com/r/99"}

    monkeypatch.setattr("aismm.tokens.valid_access_token",
                        lambda *a, **k: _async("tok"))
    env["instruction"].publish_mode = PublishMode.live
    monkeypatch.setattr(
        "aismm.platforms.registry.get_platform",
        lambda name: _FakePlatform(_fake_reply))

    result = _reply(env)
    assert result["status"] == "replied" and result["url"].endswith("/r/99")
    assert sent["target_id"] == "c1"
    # The ledger now knows this target — the essential cron guard.
    assert engagement_ledger.answered(env["account"], "comment", "c1")


# --- the duplicate guards ------------------------------------------------------------ #

def test_a_target_already_answered_is_skipped(env):
    engagement_ledger.record(env["account"], env["store"], "comment", "c1",
                             url="https://x/y")
    result = _reply(env)
    assert result["status"] == "skipped" and result["already_answered"] is True
    # Nothing new was staged.
    assert env["store"].list_staged(pending_only=False) == []
    assert env["engagement"]["skipped"] == 1


def test_a_target_already_staged_is_not_restaged(env):
    _reply(env)                                   # first run stages a preview
    assert len(env["store"].list_staged(pending_only=False)) == 1
    result = _reply(env)                          # a later run re-reads the same comment
    assert result["status"] == "skipped" and result["already_staged"] is True
    assert len(env["store"].list_staged(pending_only=False)) == 1  # still just the one


def test_a_rejected_reply_can_be_reconsidered(env):
    _reply(env)
    staged = env["store"].list_staged(pending_only=False)[0]
    staged.status = StagedStatus.rejected
    env["store"].update_staged(staged)
    # A rejected reply is not "open", so the same comment may be staged again.
    result = _reply(env)
    assert result["status"] == "staged"


def test_empty_reply_and_missing_target_are_refused(env):
    assert _reply(env, text="  ")["error"] == "empty_reply"
    assert _reply(env, target_id="")["error"] == "no_target"


# --- helpers ------------------------------------------------------------------------- #

def _async(value):
    async def _c():
        return value
    return _c()


class _FakePlatform:
    def __init__(self, reply):
        self._reply = reply

    class capabilities:  # noqa: N801 - matches attribute access platform.capabilities.x
        supports_comments = True

    async def reply_to_target(self, access_token, account, *, target_type, target_id, text):
        return await self._reply(access_token, account, target_type=target_type,
                                 target_id=target_id, text=text)
