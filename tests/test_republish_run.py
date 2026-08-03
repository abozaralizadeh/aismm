"""Publishing a failed run's media again, without re-running the agent.

The usual reason a run fails is not that the content was wrong — it is that the
*publish* was refused: a rate limit, an expired token, X out of API credits. The
agent retry regenerates a Sora clip or an image for that, which costs minutes and
money and produces different content than the one that was already reviewed.

Republish sends the exact caption, media and placement the failed run recorded
straight through the publish gate. No model call anywhere.
"""
import time

import pytest

from aismm import orchestrator
from aismm.dashboard import app as app_module
from aismm.models import Account, Instruction, PlatformName, PublishMode, Run, RunStatus

CAPTION = "Panel 7 — the door opens."


@pytest.fixture()
def setup(store, monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "get_store", lambda: store)
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="genaicomicbook",
                external_id="1"), access_token="t")
    instruction = store.upsert_instruction(
        Instruction(name="Comicbook", publish_mode=PublishMode.dry_run,
                    account_ids_json=f'["{account.id}"]'))

    panel = tmp_path / "panel7.jpg"
    panel.write_bytes(b"\xff\xd8\xff\xe0jpegbytes")
    failed = Run(instruction_id=instruction.id, account_id=account.id,
                 status=RunStatus.failed, error="rate limited",
                 caption=CAPTION, asset_path=str(panel), placement="feed")
    failed.set_asset_paths([str(panel)])
    failed = store.add_run(failed)

    seen = {}

    async def fake_publish(state, caption, **kwargs):
        seen["caption"] = caption
        seen["kwargs"] = kwargs
        seen["run_id"] = state["run"].id
        return {"mode": "dry_run", "status": "preview"}

    monkeypatch.setattr("aismm.tools.publish_tool.perform_publish", fake_publish)

    def no_agent(*_args, **_kwargs):
        raise AssertionError("republish must never invoke the agent")

    monkeypatch.setattr(orchestrator, "run_for_account", no_agent)
    return instruction, account, failed, str(panel), seen


# --- the orchestrator entry point ---------------------------------------------------- #

def test_the_recorded_media_and_caption_are_sent_verbatim(store, setup):
    _instruction, _account, failed, panel, seen = setup
    orchestrator.republish_run(failed.id)
    assert seen["caption"] == CAPTION
    assert seen["kwargs"]["asset_path"] == panel
    assert seen["kwargs"]["placement"] == "feed"


def test_an_edited_caption_overrides_the_recorded_one(store, setup):
    _instruction, _account, failed, _panel, seen = setup
    orchestrator.republish_run(failed.id, "Rewritten wording.")
    assert seen["caption"] == "Rewritten wording."


def test_a_carousel_keeps_every_item(store, setup, tmp_path):
    instruction, account, _failed, panel, seen = setup
    second = tmp_path / "panel8.jpg"
    second.write_bytes(b"\xff\xd8\xff\xe0more")
    run = Run(instruction_id=instruction.id, account_id=account.id,
              status=RunStatus.failed, caption=CAPTION, asset_path=panel)
    run.set_asset_paths([panel, str(second)])
    run = store.add_run(run)
    orchestrator.republish_run(run.id)
    assert seen["kwargs"]["asset_paths"] == [panel, str(second)]


def test_the_placement_is_preserved(store, setup):
    instruction, account, _failed, panel, seen = setup
    run = Run(instruction_id=instruction.id, account_id=account.id,
              status=RunStatus.failed, caption="", asset_path=panel, placement="story")
    run.set_asset_paths([panel])
    run = store.add_run(run)
    orchestrator.republish_run(run.id)
    assert seen["kwargs"]["placement"] == "story"


def test_the_republish_is_a_new_run(store, setup):
    """The failed attempt is evidence; it must stay readable."""
    _instruction, _account, failed, _panel, seen = setup
    orchestrator.republish_run(failed.id)
    assert seen["run_id"] != failed.id
    unchanged = store.get_run(failed.id)
    assert unchanged.status is RunStatus.failed and unchanged.error == "rate limited"


def test_no_agent_is_involved(store, setup):
    """The fixture's run_for_account raises if it is ever called."""
    _instruction, _account, failed, _panel, _seen = setup
    assert "error" not in orchestrator.republish_run(failed.id)


def test_republishing_an_unknown_run(store, setup):
    assert orchestrator.republish_run("nope")["error"] == "not_found"


def test_republishing_when_the_instruction_was_deleted(store, setup):
    instruction, _account, failed, _panel, _seen = setup
    store.delete_instruction(instruction.id)
    assert orchestrator.republish_run(failed.id)["error"] == "instruction_missing"


def test_republishing_when_the_account_was_disconnected(store, setup, monkeypatch):
    _instruction, _account, failed, _panel, _seen = setup
    monkeypatch.setattr(store, "get_account", lambda _id: None)
    assert orchestrator.republish_run(failed.id)["error"] == "account_missing"


def test_a_run_with_neither_caption_nor_media(store, setup):
    instruction, account, _failed, _panel, _seen = setup
    empty = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                              status=RunStatus.failed))
    assert orchestrator.republish_run(empty.id)["error"] == "nothing_to_publish"


def test_media_that_no_longer_exists_is_refused(store, setup):
    """Better a clear refusal than a publish of whatever is at that path now."""
    instruction, account, _failed, _panel, _seen = setup
    run = Run(instruction_id=instruction.id, account_id=account.id,
              status=RunStatus.failed, caption=CAPTION, asset_path="/gone/panel.jpg")
    run.set_asset_paths(["/gone/panel.jpg"])
    run = store.add_run(run)
    result = orchestrator.republish_run(run.id)
    assert result["error"] == "media_gone"
    assert "agent retry" in result["message"]


def test_a_caption_only_run_needs_no_media(store, setup):
    """X posts text; there is nothing on disk to check."""
    instruction, account, _failed, _panel, seen = setup
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            status=RunStatus.failed, caption="just words"))
    assert "error" not in orchestrator.republish_run(run.id)
    assert seen["kwargs"]["media_kind"] == "text"


def test_a_live_account_in_cooldown_is_refused(store, setup, monkeypatch):
    """Knocking again during a Meta block is what extends the block."""
    instruction, account, failed, _panel, _seen = setup
    instruction.publish_mode = PublishMode.live
    store.upsert_instruction(instruction)
    monkeypatch.setattr(orchestrator.cooldown, "is_active", lambda _a: True)
    monkeypatch.setattr(orchestrator.cooldown, "describe", lambda _a: "42 minutes")
    result = orchestrator.republish_run(failed.id)
    assert result["error"] == "rate_limited" and "42 minutes" in result["message"]


def test_a_dry_run_ignores_the_cooldown(store, setup, monkeypatch):
    """It calls no platform API, so there is nothing to be blocked for."""
    _instruction, _account, failed, _panel, _seen = setup
    monkeypatch.setattr(orchestrator.cooldown, "is_active", lambda _a: True)
    assert "error" not in orchestrator.republish_run(failed.id)


def test_a_publish_failure_marks_the_new_run_failed(store, setup, monkeypatch):
    _instruction, _account, failed, _panel, _seen = setup

    async def boom(*_a, **_k):
        raise RuntimeError("token expired")

    monkeypatch.setattr("aismm.tools.publish_tool.perform_publish", boom)
    result = orchestrator.republish_run(failed.id)
    assert result["error"] == "publish_failed" and "token expired" in result["message"]
    newest = [r for r in store.list_runs(limit=10) if r.id != failed.id][0]
    assert newest.status is RunStatus.failed


# --- through the dashboard ----------------------------------------------------------- #

@pytest.fixture()
def dash(store, monkeypatch):
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


def test_the_republish_route_publishes(dash, store, setup):
    _instruction, _account, failed, _panel, seen = setup
    response = dash.test_client().post(f"/runs/{failed.id}/republish",
                                       data={"caption": CAPTION}, follow_redirects=True)
    assert response.status_code == 200
    for _ in range(40):                       # it runs in a background thread
        if seen:
            break
        time.sleep(0.05)
    assert seen.get("caption") == CAPTION


def test_republishing_an_unknown_run_is_404(dash, store, setup):
    assert dash.test_client().post("/runs/nope/republish", data={}).status_code == 404
