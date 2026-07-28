"""A run that cannot do its job must FAIL — not invent a post about the problem.

The bug this covers: the agent could not read the source page, so it generated a
video and published it with a caption explaining the difficulty. Two structural
causes (the prompt demanded a publish, and the recovery nudge ordered one) plus a
missing escape hatch (no way to say "I can't").
"""
import asyncio

import pytest

from aismm.agent.prompts import MANAGER_INSTRUCTIONS
from aismm.models import Account, Instruction, PlatformName, PublishMode, Run, RunStatus
from aismm.tools.failure_tool import perform_report_failure
from aismm.tools.publish_tool import meta_caption_reason, perform_publish


@pytest.fixture()
def run_state(store):
    account = store.upsert_account(Account(platform=PlatformName.twitter, handle="t",
                                           external_id="1"), access_token="x")
    instruction = store.upsert_instruction(Instruction(name="Comic crawl",
                                                       publish_mode=PublishMode.dry_run))
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id))
    return {"account": account, "instruction": instruction, "store": store, "run": run,
            "assets": []}


# --- report_failure ----------------------------------------------------------------- #

def test_report_failure_marks_the_run_failed_without_posting(run_state):
    result = asyncio.run(perform_report_failure(
        run_state, "the comic page returned no panel images for 2026-05-13",
        details="browse_page returned 0 images; text was 'Generating…'",
        next_step="retry once the page renders panels"))

    assert result["status"] == "failed"
    store, run = run_state["store"], run_state["run"]
    assert store.list_staged() == []                       # nothing queued or posted
    assert run.status is RunStatus.failed
    assert "no panel images" in run.error


def test_report_failure_keeps_the_diagnosis_for_debugging(run_state):
    asyncio.run(perform_report_failure(run_state, "source unreachable",
                                       details="HTTP 503 from example.com/feed",
                                       next_step="skip to the next date"))
    log = run_state["run"].log
    assert "HTTP 503 from example.com/feed" in log
    assert "skip to the next date" in log


def test_report_failure_is_a_terminal_result(run_state):
    """The agent loop must treat it as an ending, like publish."""
    asyncio.run(perform_report_failure(run_state, "nothing new to post"))
    assert run_state["result"]["mode"] == "failed"


def test_report_failure_without_a_reason_still_records_something(run_state):
    asyncio.run(perform_report_failure(run_state, "   "))
    assert run_state["run"].error


# --- the caption guard --------------------------------------------------------------- #

@pytest.mark.parametrize("caption", [
    "I was unable to retrieve the comic panel for today, so here is a generated video.",
    "Could not load the page, but enjoy this scene!",
    "Failed to fetch the image — here's something else instead.",
    "browse_page returned no images for this date.",
    "As an AI language model, I cannot access that page.",
    "I apologise — technical difficulties today.",
    "Placeholder post while the source is down.",
])
def test_captions_about_the_failure_are_refused(run_state, caption):
    result = asyncio.run(perform_publish(run_state, caption))
    assert result["error"] == "caption_describes_a_failure"
    assert "report_failure" in result["message"]
    assert run_state["store"].list_staged() == []          # nothing was staged


@pytest.mark.parametrize("caption", [
    "I couldn't believe the sunrise over the harbour this morning.",
    "The mission failed to reach orbit, engineers said today.",
    "Panel 1: At dawn, Nerina drags Mira up the sealed lighthouse.",
    "Rescue crews could not find survivors, officials confirmed.",  # news about others
    "How to generate more leads in 2026.",
    "",
])
def test_ordinary_captions_are_not_blocked(run_state, caption):
    """The guard must be narrow — blocking real posts would be worse than the bug."""
    assert meta_caption_reason(caption) == ""


def test_guard_can_be_disabled(run_state, monkeypatch):
    import aismm.tools.publish_tool as module
    import dataclasses
    from aismm import config as config_module

    monkeypatch.setattr(module, "settings",
                        dataclasses.replace(config_module.settings,
                                            publish_content_guard=False))
    result = asyncio.run(perform_publish(run_state, "I was unable to fetch the page."))
    assert result.get("error") != "caption_describes_a_failure"


def test_a_real_post_still_publishes(run_state):
    result = asyncio.run(perform_publish(run_state, "Panel 3: the archive of storm-kites."))
    assert result["status"] == "staged"
    assert len(run_state["store"].list_staged()) == 1


# --- the prompt no longer demands a publish -------------------------------------------- #

def test_prompt_offers_report_failure_as_a_terminal_option():
    assert "report_failure" in MANAGER_INSTRUCTIONS
    assert "Publishing is NOT mandatory" in MANAGER_INSTRUCTIONS
    assert "NEVER publish a post about the problem itself" in MANAGER_INSTRUCTIONS


def test_prompt_no_longer_orders_an_unconditional_publish():
    """The old wording ('Always finish by calling publish') caused the bug."""
    assert "Always finish by calling publish" not in MANAGER_INSTRUCTIONS
