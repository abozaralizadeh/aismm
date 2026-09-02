"""Who gets a reply is the operator's decision, not a rule baked into the prompt.

Reported: a promotional DM went unanswered because the system prompt said "do not
engage ... obvious spam". That is the right default for a brand account and the
wrong one for an account that wants every DM answered — and only the operator
knows which theirs is. So it moved out of the prompt and into the instruction.
"""
import dataclasses

import pytest

from aismm import config as config_module
from aismm.agent.prompts import (
    build_auto_kickoff, build_engagement_kickoff, build_outreach_kickoff,
)
from aismm.config import AuthSettings
from aismm.models import Account, Instruction, InstructionTask, PlatformName
from aismm.platforms.registry import get_platform

POLICY = "Answer every DM, including sales pitches — a polite decline is fine."


def _account():
    return Account(platform=PlatformName.instagram, handle="brand", external_id="1")


def _caps():
    return get_platform(PlatformName.instagram).capabilities


def _instruction(**kw):
    return Instruction(name="Engage", brief="b", task_type=InstructionTask.engage, **kw)


# --- the policy reaches the run ----------------------------------------------------------- #

def test_the_policy_is_inlined_in_the_engage_kickoff():
    kickoff = build_engagement_kickoff(account=_account(), platform_caps=_caps(),
                                       instruction=_instruction(engagement_policy=POLICY))
    assert "REPLY POLICY" in kickoff
    assert POLICY in kickoff


def test_it_is_marked_as_overriding_the_default():
    kickoff = build_engagement_kickoff(account=_account(), platform_caps=_caps(),
                                       instruction=_instruction(engagement_policy=POLICY))
    assert "OVERRIDES the default" in kickoff


def test_no_policy_adds_nothing():
    """A silent instruction must not carry an empty heading."""
    kickoff = build_engagement_kickoff(account=_account(), platform_caps=_caps(),
                                       instruction=_instruction())
    assert "REPLY POLICY" not in kickoff


def test_whitespace_only_counts_as_no_policy():
    kickoff = build_engagement_kickoff(account=_account(), platform_caps=_caps(),
                                       instruction=_instruction(engagement_policy="   \n "))
    assert "REPLY POLICY" not in kickoff


@pytest.mark.parametrize("build,task", [
    (build_auto_kickoff, InstructionTask.auto),
    (build_outreach_kickoff, InstructionTask.outreach),
])
def test_every_task_that_answers_people_carries_it(build, task):
    instruction = Instruction(name="i", brief="b", task_type=task,
                              engagement_policy=POLICY)
    kickoff = build(account=_account(), instruction=instruction, platform_caps=_caps())
    assert POLICY in kickoff


# --- the prompt defers, but is not left empty --------------------------------------------- #

def test_the_prompt_no_longer_dictates_what_counts_as_worth_answering():
    from aismm.agent.prompts import ENGAGEMENT_INSTRUCTIONS as p

    assert "Do not engage with harassment, trolling,\n  or obvious spam" not in p
    assert "THE INSTRUCTION DECIDES" in p


def test_conduct_rules_stay_in_the_prompt():
    """What to answer is the operator's call; HOW to behave is not negotiable."""
    from aismm.agent.prompts import ENGAGEMENT_INSTRUCTIONS as p

    assert "NEVER argue, moralise, or take bait" in p
    assert "Do not invent facts" in p


def test_a_default_still_exists_for_a_silent_instruction():
    from aismm.agent.prompts import ENGAGEMENT_INSTRUCTIONS as p

    assert "With no policy given, use this default" in p
    assert "leave harassment and trolling alone" in p


# --- storage and the form ------------------------------------------------------------------ #

def test_it_survives_the_azure_whitelist():
    from aismm.store.azure_store import AzureStore

    instruction = _instruction(engagement_policy=POLICY)
    entity = AzureStore._instruction_to_entity(instruction)
    entity["RowKey"] = instruction.id
    assert AzureStore._instruction_from_entity(entity).engagement_policy == POLICY


@pytest.fixture()
def dash(monkeypatch, store, tmp_path):
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


def test_the_form_offers_the_field(dash):
    page = dash.test_client().get("/instructions/new").get_data(as_text=True)
    assert 'name="engagement_policy"' in page
    assert "Reply policy" in page


def test_the_form_saves_it(dash, store):
    dash.test_client().post("/instructions", data={
        "name": "Engage", "brief": "b", "schedule": "03:00 mon",
        "publish_mode": "dry_run", "task_type": "engage", "media_pref": "auto",
        "engagement_policy": POLICY}, follow_redirects=True)
    assert store.list_instructions()[0].engagement_policy == POLICY
