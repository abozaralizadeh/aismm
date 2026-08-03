"""Retrying a failed run, with its prompt editable before it goes again.

A run stores the exact kickoff it received. When one fails for a reason the
prompt itself caused — a stale memory position, a brief that pointed at the wrong
page — the fix is to send it again with that text corrected, without editing the
instruction and without waiting for the next scheduled fire.
"""
import time

import pytest

from aismm import orchestrator
from aismm.dashboard import app as app_module
from aismm.models import Account, Instruction, PlatformName, PublishMode, Run, RunStatus

ORIGINAL = "ORIGINAL PROMPT\nCURRENT POSITION: Panel 6.\nNEXT: Panel 7."
EDITED = "EDITED PROMPT\nCURRENT POSITION: Panel 7.\nNEXT: Panel 8."


@pytest.fixture()
def setup(store, monkeypatch):
    # retry_run resolves the store itself, so point it at the test one.
    monkeypatch.setattr(orchestrator, "get_store", lambda: store)
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="genaicomicbook",
                external_id="1"), access_token="t")
    instruction = store.upsert_instruction(
        Instruction(name="Comicbook", publish_mode=PublishMode.dry_run,
                    account_ids_json=f'["{account.id}"]'))
    failed = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                               status=RunStatus.failed, error="boom", prompt=ORIGINAL))

    seen = {}

    async def fake_agent(acct, instr, st, run, prompt_override=""):
        # Mirrors the real manager_agent: a blank/whitespace override recomposes.
        seen["prompt"] = prompt_override.strip() or "(recomposed from the instruction)"
        seen["run_id"] = run.id
        return {"mode": "dry_run"}

    monkeypatch.setattr(orchestrator, "run_for_account", fake_agent)
    return instruction, account, failed, seen


# --- the orchestrator entry point ---------------------------------------------------- #

def test_an_edited_prompt_is_sent_verbatim(store, setup):
    _instruction, _account, failed, seen = setup
    orchestrator.retry_run(failed.id, EDITED)
    assert seen["prompt"] == EDITED


def test_an_empty_prompt_recomposes_from_the_instruction(store, setup):
    _instruction, _account, failed, seen = setup
    orchestrator.retry_run(failed.id, "   ")
    assert seen["prompt"] == "(recomposed from the instruction)"


def test_the_retry_is_a_new_run(store, setup):
    """The failed attempt is evidence; it must stay readable."""
    _instruction, _account, failed, seen = setup
    orchestrator.retry_run(failed.id, EDITED)
    assert seen["run_id"] != failed.id
    assert len(store.list_runs(limit=10)) == 2


def test_the_original_run_is_untouched(store, setup):
    _instruction, _account, failed, _seen = setup
    orchestrator.retry_run(failed.id, EDITED)
    unchanged = store.get_run(failed.id)
    assert unchanged.status is RunStatus.failed
    assert unchanged.prompt == ORIGINAL
    assert unchanged.error == "boom"


def test_retrying_an_unknown_run(store, setup):
    assert orchestrator.retry_run("nope", "x")["error"] == "not_found"


def test_retrying_when_the_instruction_was_deleted(store, setup):
    instruction, _account, failed, _seen = setup
    store.delete_instruction(instruction.id)
    result = orchestrator.retry_run(failed.id, EDITED)
    assert result["error"] == "instruction_missing"


def test_retrying_when_the_account_was_disconnected(store, setup, monkeypatch):
    _instruction, account, failed, _seen = setup
    monkeypatch.setattr(store, "get_account", lambda _id: None)
    result = orchestrator.retry_run(failed.id, EDITED)
    assert result["error"] == "account_missing"


def test_a_retry_obeys_the_publish_mode_gate(store, setup):
    """It re-runs the instruction as configured — it does not force a live post."""
    instruction, _account, failed, _seen = setup
    assert instruction.publish_mode is PublishMode.dry_run
    result = orchestrator.retry_run(failed.id, EDITED)
    assert result.get("mode") == "dry_run"


def test_a_retry_is_still_single_flighted(store, setup):
    """It goes through _run_one, so the per-account lock applies as usual."""
    instruction, account, failed, _seen = setup
    lock_key = f"instr:{instruction.id}:acct:{account.id}"
    assert store.acquire_lock(lock_key, ttl_seconds=300)
    result = orchestrator.retry_run(failed.id, EDITED)
    assert result["status"] == "skipped" and result["reason"] == "locked"


# --- through the dashboard ----------------------------------------------------------- #

@pytest.fixture()
def dash(store, monkeypatch):
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


def test_the_retry_route_starts_a_run(dash, store, setup):
    _instruction, _account, failed, seen = setup
    response = dash.test_client().post(f"/runs/{failed.id}/retry",
                                       data={"prompt": EDITED}, follow_redirects=True)
    assert response.status_code == 200
    for _ in range(40):                       # it runs in a background thread
        if seen:
            break
        time.sleep(0.05)
    assert seen.get("prompt") == EDITED


def test_retrying_an_unknown_run_is_404(dash, store, setup):
    assert dash.test_client().post("/runs/nope/retry", data={"prompt": "x"}).status_code == 404


def test_the_detail_page_offers_a_retry_prefilled_with_the_prompt(dash, store, setup):
    _instruction, _account, failed, _seen = setup
    page = dash.test_client().get(f"/runs/{failed.id}").get_data(as_text=True)
    assert "Re-run the agent" in page
    assert 'name="prompt"' in page
    assert "CURRENT POSITION: Panel 6." in page       # the original, editable


def test_the_republish_form_is_open_for_a_failed_run(dash, store, setup):
    """It is the thing you came to the page for; don't make them hunt for it."""
    instruction, account, _failed, _seen = setup
    failed = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                               status=RunStatus.failed, caption="ready to go",
                               asset_path="/a.jpg", prompt=ORIGINAL))
    page = dash.test_client().get(f"/runs/{failed.id}").get_data(as_text=True)
    assert 'class="prompt-block retry-block" open' in page
    assert "Publish this again" in page


def test_the_republish_form_is_collapsed_for_a_published_run(dash, store, setup):
    instruction, account, _failed, _seen = setup
    ok = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                           status=RunStatus.published, caption="c", asset_path="/a.jpg",
                           prompt=ORIGINAL))
    page = dash.test_client().get(f"/runs/{ok.id}").get_data(as_text=True)
    assert "Publish this again" in page
    assert 'class="prompt-block retry-block" open' not in page


def test_a_live_instruction_warns_that_a_retry_posts_for_real(dash, store, setup):
    instruction, account, _failed, _seen = setup
    instruction.publish_mode = PublishMode.live
    store.upsert_instruction(instruction)
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            status=RunStatus.failed, prompt=ORIGINAL))
    page = dash.test_client().get(f"/runs/{run.id}").get_data(as_text=True)
    assert "a successful retry posts for real" in page
