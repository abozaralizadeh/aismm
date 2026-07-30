"""Volume refusals: classify, back off, and don't lose the agent's place.

The live failure: Instagram returned ``code=4 · error_subcode=2207051 ·
error_user_title=action is blocked`` after the agent had browsed, downloaded a
panel, converted it and written its memory. Three things were wrong — the error
looked like any other publish failure, the next hourly run would hit the same wall
(which *extends* a Meta block), and the memory had already been advanced past a
panel that was never posted.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from aismm import cooldown
from aismm.agent.prompts import MANAGER_INSTRUCTIONS
from aismm.models import Account, Instruction, PlatformName, PublishMode, Run
from aismm.platforms.instagram import RateLimited, _graph_error, _raise_graph
from aismm.tools.publish_tool import perform_publish

IG_USER = "17841400000000000"


def _graph_error_response(code, subcode=None, message="Application request limit reached"):
    request = httpx.Request("POST", "https://graph.facebook.com/v21.0/x/media")
    body = {"error": {"message": message, "type": "OAuthException", "code": code,
                      "error_user_title": "action is blocked"}}
    if subcode is not None:
        body["error"]["error_subcode"] = subcode
    response = httpx.Response(403, request=request, json=body)
    return httpx.HTTPStatusError("403", request=request, response=response)


# --- classification ----------------------------------------------------------------- #

@pytest.mark.parametrize("code", [4, 17, 32])
def test_volume_codes_raise_rate_limited(code):
    with pytest.raises(RateLimited):
        _raise_graph(_graph_error_response(code))


def test_the_exact_live_error_is_recognised():
    """code=4 · error_subcode=2207051 · 'action is blocked'."""
    with pytest.raises(RateLimited) as exc:
        _raise_graph(_graph_error_response(4, 2207051))
    assert "Application request limit reached" in str(exc.value)
    assert exc.value.retry_after_seconds > 0


def test_the_blocked_subcode_alone_is_enough():
    with pytest.raises(RateLimited):
        _raise_graph(_graph_error_response(190, 2207051))


@pytest.mark.parametrize("code,subcode", [(352, 2207026), (100, None), (9007, None)])
def test_content_errors_are_not_rate_limits(code, subcode):
    """A wrong aspect ratio must stay a content error — retrying that IS correct."""
    with pytest.raises(RuntimeError) as exc:
        _raise_graph(_graph_error_response(code, subcode, "The media is not eligible"))
    assert not isinstance(exc.value, RateLimited)


def test_the_error_body_is_still_surfaced():
    message, err = _graph_error(_graph_error_response(4, 2207051))
    assert "code=4" in message and "error_subcode=2207051" in message


# --- the cooldown ------------------------------------------------------------------- #

def test_a_fresh_account_has_no_cooldown(store):
    account = Account(platform=PlatformName.instagram, external_id=IG_USER)
    assert cooldown.is_active(account) is False
    assert cooldown.remaining_seconds(account) == 0


def test_starting_a_cooldown_blocks_the_account(store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    cooldown.start(account, store, 3600, reason="test")
    assert cooldown.is_active(account)
    assert 3500 < cooldown.remaining_seconds(account) <= 3600
    # ...and it survives a reload from the store.
    assert cooldown.is_active(store.get_account(account.id))


def test_a_cooldown_extends_rather_than_shortens(store):
    """Two refusals mean the platform is less happy, not more."""
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    cooldown.start(account, store, 7200)
    cooldown.start(account, store, 600)
    assert cooldown.remaining_seconds(account) > 7000


def test_an_expired_cooldown_is_inactive(store):
    account = Account(platform=PlatformName.instagram, external_id=IG_USER)
    account.set_meta({cooldown.META_KEY:
                      (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()})
    assert cooldown.is_active(account) is False


def test_a_corrupt_deadline_is_ignored_rather_than_crashing(store):
    account = Account(platform=PlatformName.instagram, external_id=IG_USER)
    account.set_meta({cooldown.META_KEY: "not-a-date"})
    assert cooldown.is_active(account) is False


def test_clearing_a_cooldown(store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    cooldown.start(account, store, 3600)
    cooldown.clear(account, store)
    assert cooldown.is_active(store.get_account(account.id)) is False


def test_a_cooldown_does_not_destroy_the_stored_token(store):
    """It writes through upsert_account — the token must survive."""
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER),
        access_token="page-token")
    cooldown.start(account, store, 60)
    assert store.get_tokens(account.id)[0] == "page-token"


def test_describe_is_human_readable(store):
    account = Account(platform=PlatformName.instagram, external_id=IG_USER)
    cooldown.start(account, _NullStore(), 5400)
    assert cooldown.describe(account) in {"1h 29m", "1h 30m"}


class _NullStore:
    def upsert_account(self, account, **kwargs):
        return account


# --- publish sets the cooldown and reports it distinctly ----------------------------- #

def _publish_with(monkeypatch, store, exc, *, schedule="", tmp_path=None):
    from aismm.platforms import registry

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="clinic", external_id=IG_USER),
        access_token="t")
    instruction = store.upsert_instruction(
        Instruction(name="Comicbook", publish_mode=PublishMode.live, schedule=schedule))
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id))

    asset = tmp_path / "panel.jpg"
    asset.write_bytes(b"\xff\xd8\xffx")

    platform = registry.get_platform(PlatformName.instagram)

    async def refuse(**kwargs):
        raise exc

    monkeypatch.setattr(platform, "publish", refuse)
    monkeypatch.setattr(registry, "get_platform", lambda *a, **kw: platform)
    monkeypatch.setattr("aismm.tools.publish_tool.media.normalize_image",
                        lambda data, **kw: data)

    state = {"account": account, "instruction": instruction, "store": store, "run": run,
             "assets": []}
    result = asyncio.run(perform_publish(state, "Panel 4 of 2026-05-17",
                                         asset_path=str(asset), media_kind="image"))
    return result, account, run


def test_a_rate_limit_is_reported_as_such_not_as_a_generic_failure(store, monkeypatch, tmp_path):
    result, _account, _run = _publish_with(
        monkeypatch, store, RateLimited("Instagram Graph 403: Application request limit reached"),
        tmp_path=tmp_path)
    assert result["error"] == "rate_limited"
    assert result["retry_after_minutes"] > 0
    assert "volume reasons" in result["message"]


def test_a_rate_limit_starts_a_cooldown(store, monkeypatch, tmp_path):
    _result, account, _run = _publish_with(
        monkeypatch, store, RateLimited("limit"), tmp_path=tmp_path)
    assert cooldown.is_active(store.get_account(account.id))


def test_a_frequent_schedule_is_named_as_the_likely_cause(store, monkeypatch, tmp_path):
    result, _account, _run = _publish_with(
        monkeypatch, store, RateLimited("limit"), schedule="every 1h", tmp_path=tmp_path)
    assert "every 1h" in result["message"]
    assert "Lengthen the schedule" in result["message"]


def test_a_sane_schedule_gets_no_lecture(store, monkeypatch, tmp_path):
    result, _account, _run = _publish_with(
        monkeypatch, store, RateLimited("limit"), schedule="0 9 * * *", tmp_path=tmp_path)
    assert "Lengthen the schedule" not in result["message"]


def test_a_content_failure_does_not_start_a_cooldown(store, monkeypatch, tmp_path):
    """Only volume refusals pause the account; a bad image should be retried."""
    _result, account, _run = _publish_with(
        monkeypatch, store, RuntimeError("Instagram Graph 400: media not eligible"),
        tmp_path=tmp_path)
    assert cooldown.is_active(store.get_account(account.id)) is False


# --- the orchestrator skips a cooling-down account BEFORE doing the work ------------- #

def test_a_live_run_is_skipped_while_rate_limited(store, monkeypatch):
    from aismm import orchestrator

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    cooldown.start(account, store, 3600)
    instruction = store.upsert_instruction(
        Instruction(name="Comicbook", publish_mode=PublishMode.live))

    def explode(*args, **kwargs):
        raise AssertionError("the agent must not run while the account is blocked")

    monkeypatch.setattr(orchestrator, "run_for_account", explode)
    result = orchestrator._run_one(instruction, store.get_account(account.id), store)

    assert result["status"] == "skipped" and result["reason"] == "rate_limited"
    assert result["retry_in"]


def test_a_dry_run_still_proceeds_while_rate_limited(store, monkeypatch):
    """A preview touches no API, so there's no reason to block it."""
    from aismm import orchestrator

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    cooldown.start(account, store, 3600)
    instruction = store.upsert_instruction(
        Instruction(name="Comicbook", publish_mode=PublishMode.dry_run))

    async def ran(*args, **kwargs):
        return {"mode": "dry_run", "ok": True}

    monkeypatch.setattr(orchestrator, "run_for_account", ran)
    result = orchestrator._run_one(instruction, store.get_account(account.id), store)
    assert result.get("ok") is True


def test_no_run_row_is_created_for_a_skipped_account(store, monkeypatch):
    from aismm import orchestrator

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    cooldown.start(account, store, 3600)
    instruction = store.upsert_instruction(
        Instruction(name="Comicbook", publish_mode=PublishMode.live))
    before = store.count_runs()
    orchestrator._run_one(instruction, store.get_account(account.id), store)
    assert store.count_runs() == before


# --- the prompt must not advance the position past an unpublished item --------------- #

def test_the_prompt_writes_memory_after_publish_not_before():
    """The live run did `update_memory, publish` and was about to skip Panel 4."""
    steps = MANAGER_INSTRUCTIONS
    publish_at = steps.index("9. Call publish once")
    outcome_at = steps.index("10. Read what publish RETURNED")
    assert publish_at < outcome_at


def test_the_prompt_says_a_refused_post_leaves_the_position_alone():
    assert "rate limits included" in MANAGER_INSTRUCTIONS
    assert "Never advance past something you did not post" in MANAGER_INSTRUCTIONS
