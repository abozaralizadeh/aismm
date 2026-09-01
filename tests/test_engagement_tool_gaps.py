"""A run must not report finding nothing when it could not look.

Reported live, on an account with unanswered DMs:

    Engagement done: 0 replied, 0 staged, 0 skipped. Scanned 12 recent
    posts/reels, mentions, and inbound DMs; no new comments or DMs needed
    replies…

The DM tools were never in that run's tool set. `Instruction.tools_json` narrows
what `build_tools` offers, and a list ticked before a tool existed never receives
it — so the run truthfully found nothing, having no way to look, and said so in
words that read as "your inbox is empty".
"""
import dataclasses

import pytest

from aismm import config as config_module
from aismm.agent.manager_agent import _engagement_gaps
from aismm.agent.prompts import build_auto_kickoff, build_engagement_kickoff
from aismm.config import AuthSettings
from aismm.models import Account, Instruction, InstructionTask, PlatformName
from aismm.platforms.registry import get_platform
from aismm.tools.registry import build_tools


def _account(platform=PlatformName.instagram):
    account = Account(platform=platform, handle="genaicomicbook", external_id="178414")
    account.set_meta({"page_id": "999"})
    return account


def _caps(platform=PlatformName.instagram):
    return get_platform(platform).capabilities


# --- the trap itself --------------------------------------------------------------------- #

def test_a_narrowed_tool_list_silently_excludes_a_newer_tool():
    """The mechanism behind the report. Empty means all; narrowed means exactly
    what was ticked, for ever."""
    account = _account()
    narrowed = Instruction(name="e", brief="b", task_type=InstructionTask.engage,
                           tools_json='["instagram_recent_comments"]')
    state = {"account": account, "instruction": narrowed, "store": None, "run": None,
             "assets": [], "result": {}}
    names = {t.name for t in build_tools(state, narrowed.tools)}
    assert "instagram_dms" not in names

    everything = Instruction(name="e", brief="b", task_type=InstructionTask.engage)
    state["instruction"] = everything
    assert "instagram_dms" in {t.name for t in build_tools(state, everything.tools)}


# --- so the run is told what it cannot do ------------------------------------------------- #

def test_a_missing_dm_tool_is_reported_as_a_gap():
    gaps = _engagement_gaps(_caps(), {"instagram_recent_comments"}, _account())
    assert any("direct messages" in gap for gap in gaps)
    assert any("instagram_dms" in gap for gap in gaps)


def test_nothing_is_reported_when_the_tools_are_all_there():
    gaps = _engagement_gaps(_caps(), {"instagram_recent_comments", "instagram_dms"},
                            _account())
    assert gaps == []


def test_a_platform_without_the_capability_is_not_a_gap():
    """TikTok has no comment API for third-party apps — that is not a missing tool."""
    gaps = _engagement_gaps(_caps(PlatformName.tiktok), set(), _account(PlatformName.tiktok))
    assert gaps == []


def test_the_kickoff_tells_the_agent_not_to_claim_it_checked():
    kickoff = build_engagement_kickoff(
        account=_account(), instruction=Instruction(name="e", brief="b"),
        platform_caps=_caps(), unavailable=["direct messages (no `instagram_dms` tool this run)"])
    assert "NOT AVAILABLE THIS RUN" in kickoff
    assert "Do NOT say you checked them" in kickoff
    assert "instagram_dms" in kickoff


def test_the_kickoff_says_to_report_the_gap_so_the_operator_can_fix_it():
    kickoff = build_engagement_kickoff(
        account=_account(), instruction=Instruction(name="e", brief="b"),
        platform_caps=_caps(), unavailable=["direct messages (no `instagram_dms` tool this run)"])
    assert "so the operator can enable it" in kickoff


def test_no_block_when_there_is_no_gap():
    """A run with everything must not carry a paragraph about nothing."""
    kickoff = build_engagement_kickoff(
        account=_account(), instruction=Instruction(name="e", brief="b"),
        platform_caps=_caps(), unavailable=[])
    assert "NOT AVAILABLE" not in kickoff


def test_the_engage_kickoff_asks_for_dms_explicitly():
    """"if a DM tool is available" invited the agent to skip them."""
    kickoff = build_engagement_kickoff(
        account=_account(), instruction=Instruction(name="e", brief="b"),
        platform_caps=_caps())
    assert "AND its inbound DMs" in kickoff
    assert "every read tool you have" in kickoff


def test_the_auto_kickoff_carries_the_same_warning():
    kickoff = build_auto_kickoff(
        account=_account(), instruction=Instruction(name="e", brief="b"),
        platform_caps=_caps(), unavailable=["direct messages (no `instagram_dms` tool this run)"])
    assert "NOT AVAILABLE THIS RUN" in kickoff


# --- and the operator is told, where they can fix it -------------------------------------- #

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


def _engage_instruction(store, tools_json):
    account = store.upsert_account(_account(), access_token="t")
    instruction = Instruction(name="Engage", brief="b", schedule="03:00 mon",
                              task_type=InstructionTask.engage, tools_json=tools_json)
    instruction.set_account_ids([account.id])
    return store.upsert_instruction(instruction)


def test_the_edit_page_warns_about_the_gap(dash, store):
    instruction = _engage_instruction(store, '["instagram_recent_comments"]')
    page = dash.test_client().get(
        f"/instructions/{instruction.id}/edit").get_data(as_text=True)
    assert "cannot see" in page and "direct messages" in page
    assert "instagram_dms" in page


def test_no_warning_when_every_tool_is_allowed(dash, store):
    """An empty selection means all tools, including ones added later."""
    instruction = _engage_instruction(store, "[]")
    page = dash.test_client().get(
        f"/instructions/{instruction.id}/edit").get_data(as_text=True)
    assert "cannot see" not in page


def test_no_warning_on_a_publishing_instruction(dash, store):
    """A publish run is not supposed to read DMs."""
    account = store.upsert_account(_account(), access_token="t")
    instruction = Instruction(name="Post", brief="b", schedule="03:00 mon",
                              tools_json='["generate_image"]')
    instruction.set_account_ids([account.id])
    store.upsert_instruction(instruction)
    page = dash.test_client().get(
        f"/instructions/{instruction.id}/edit").get_data(as_text=True)
    assert "cannot see" not in page
