"""Per-instruction tool selection.

Every tool is on by default. Narrowing the set keeps an instruction on task and
cuts the number of choices a smaller model has to weigh — a text-only account has
no use for the Sora tools. The one thing that must never be withheld is the
ability to END a run.
"""
import pytest

from aismm.dashboard import app as app_module
from aismm.models import Account, Instruction, InstructionTask, PlatformName, PublishMode
from aismm.tools.registry import (
    ALWAYS_ON, ALWAYS_ON_AUTO, ALWAYS_ON_ENGAGE, ALWAYS_ON_PUBLISH, always_on_for,
    build_tools, registered_tool_names,
)


def _names(tools):
    return sorted(getattr(t, "name", "?") for t in tools)


@pytest.fixture()
def state(store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="x", external_id="1"),
        access_token="t")
    instruction = store.upsert_instruction(
        Instruction(name="C", publish_mode=PublishMode.live,
                    account_ids_json=f'["{account.id}"]'))
    return {"account": account, "instruction": instruction, "store": store,
            "run": None, "assets": []}


# --- the registry honours the selection ---------------------------------------------- #

def test_no_selection_means_every_tool(state):
    assert len(build_tools(state, [])) == len(build_tools(state, None))
    assert _names(build_tools(state, [])) == _names(build_tools(state))


def test_a_subset_is_respected(state):
    built = _names(build_tools(state, ["web_search", "get_context"]))
    assert "web_search" in built and "get_context" in built
    assert "generate_video" not in built
    assert "browse_page" not in built


def test_the_terminal_tools_are_never_withheld(state):
    """A run must be able to end, whatever the operator ticked — but only with
    ITS task's terminal (a publish run gets ``publish``, not ``finish_engagement``)."""
    built = _names(build_tools(state, ["web_search"]))
    for name in ALWAYS_ON_PUBLISH:
        assert name in built, f"{name} was withheld — the run could never finish"
    # The other task's terminal is withheld outright, so the model cannot end the
    # wrong way (an engage terminal on a publish run and vice versa).
    assert "finish_engagement" not in built


def test_an_engage_run_gets_its_own_terminal_not_publish(state):
    state["instruction"].task_type = InstructionTask.engage
    built = _names(build_tools(state, ["web_search"]))
    for name in ALWAYS_ON_ENGAGE:
        assert name in built, f"{name} was withheld — the engage run could never finish"
    assert "publish" not in built, "an engage run must never be offered publish"


def test_an_auto_run_keeps_both_terminals(state):
    """Auto lets the agent choose, so it must be able to end EITHER way."""
    state["instruction"].task_type = InstructionTask.auto
    built = _names(build_tools(state, ["web_search"]))
    for name in ALWAYS_ON_AUTO:
        assert name in built, f"{name} was withheld from an auto run"
    assert "publish" in built and "finish_engagement" in built


def test_selecting_nothing_still_leaves_a_way_to_finish(state):
    assert _names(build_tools(state, list(ALWAYS_ON))) == sorted(ALWAYS_ON_PUBLISH)
    state["instruction"].task_type = InstructionTask.engage
    assert _names(build_tools(state, list(ALWAYS_ON))) == sorted(ALWAYS_ON_ENGAGE)
    state["instruction"].task_type = InstructionTask.auto
    assert _names(build_tools(state, list(ALWAYS_ON))) == sorted(ALWAYS_ON_AUTO)


def test_always_on_for_selects_by_task():
    assert always_on_for(InstructionTask.publish) == ALWAYS_ON_PUBLISH
    assert always_on_for(InstructionTask.engage) == ALWAYS_ON_ENGAGE
    assert always_on_for(InstructionTask.auto) == ALWAYS_ON_AUTO
    assert always_on_for("engage") == ALWAYS_ON_ENGAGE
    assert always_on_for("auto") == ALWAYS_ON_AUTO
    assert always_on_for(None) == ALWAYS_ON_PUBLISH  # unknown falls back to publish


def test_a_factory_may_still_opt_out_for_its_own_reasons(store):
    """Selection is separate from a tool disabling itself (Sora unconfigured,
    the Instagram tools on a non-Instagram run)."""
    account = store.upsert_account(
        Account(platform=PlatformName.twitter, handle="x", external_id="1"),
        access_token="t")
    instruction = store.upsert_instruction(Instruction(name="C"))
    built = _names(build_tools({"account": account, "instruction": instruction,
                                "store": store, "run": None, "assets": []},
                               ["instagram_comments", "get_context"]))
    assert "instagram_comments" not in built       # wrong platform, factory returned None
    assert "get_context" in built


def test_an_unknown_name_in_the_selection_is_ignored(state):
    built = _names(build_tools(state, ["get_context", "a_tool_that_was_removed"]))
    assert "get_context" in built


# --- what the form stores ------------------------------------------------------------ #

def test_all_ticked_is_stored_as_empty_meaning_all():
    """So a tool added to the registry later is picked up automatically."""
    assert app_module._selected_tools(registered_tool_names()) == []


def test_a_subset_is_stored_verbatim():
    keep = [n for n in registered_tool_names() if not n.startswith("instagram_")]
    assert sorted(app_module._selected_tools(keep)) == sorted(keep)


def test_nothing_ticked_is_not_the_same_as_everything_ticked():
    """Both look like an empty list; collapsing them would turn every tool back on."""
    stored = app_module._selected_tools([])
    assert stored != []
    assert sorted(stored) == sorted(ALWAYS_ON)


def test_the_stored_subset_round_trips_through_the_model(store):
    keep = ["get_context", "web_search"]
    instruction = store.upsert_instruction(Instruction(name="C"))
    instruction.set_tools(keep)
    store.upsert_instruction(instruction)
    assert store.get_instruction(instruction.id).tools == keep


def test_a_corrupt_tools_json_reads_as_all_rather_than_crashing():
    instruction = Instruction(name="C", tools_json="{not json")
    assert instruction.tools == []


# --- the picker's presentation ------------------------------------------------------- #

def test_the_catalog_covers_every_registered_tool():
    groups = app_module._tool_catalog([])
    listed = {t["name"] for g in groups for t in g["tools"]}
    assert listed == set(registered_tool_names())


def test_an_empty_selection_shows_everything_ticked():
    groups = app_module._tool_catalog([])
    assert all(t["checked"] for g in groups for t in g["tools"])


def test_a_subset_shows_only_those_ticked():
    groups = app_module._tool_catalog(["web_search"])
    checked = {t["name"] for g in groups for t in g["tools"] if t["checked"]}
    assert checked == {"web_search"}


def test_the_always_on_tools_are_flagged_for_the_ui():
    groups = app_module._tool_catalog(["web_search"])
    flagged = {t["name"] for g in groups for t in g["tools"] if t["always_on"]}
    assert flagged == set(ALWAYS_ON)


def test_groups_have_titles_and_no_duplicates():
    groups = app_module._tool_catalog([])
    names = [t["name"] for g in groups for t in g["tools"]]
    assert len(names) == len(set(names)), "a tool appeared in two groups"
    assert all(g["title"] for g in groups)


# --- end to end through the form ----------------------------------------------------- #

@pytest.fixture()
def dash(store, monkeypatch):
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


def _save(client, store, instruction, tools, present=True):
    account_ids = instruction.account_ids
    data = {"id": instruction.id, "name": instruction.name, "brief": "b",
            "publish_mode": "live", "media_pref": "auto", "enabled": "on",
            "account_ids": account_ids, "tools": tools}
    if present:
        data["tools_present"] = "1"
    client.post("/instructions", data=data, follow_redirects=True)
    return store.get_instruction(instruction.id)


def test_saving_a_subset_reaches_the_agent(dash, store, state):
    instruction = state["instruction"]
    keep = [n for n in registered_tool_names() if not n.startswith("instagram_")]
    saved = _save(dash.test_client(), store, instruction, keep)

    built = _names(build_tools({**state, "instruction": saved}, saved.tools))
    assert not any(n.startswith("instagram_") for n in built)
    assert "publish" in built


def test_a_post_without_the_picker_leaves_the_selection_alone(dash, store, state):
    """An older client must not silently reset the instruction to every tool."""
    instruction = state["instruction"]
    _save(dash.test_client(), store, instruction, ["get_context", "web_search"])
    before = store.get_instruction(instruction.id).tools

    _save(dash.test_client(), store, instruction, [], present=False)
    assert store.get_instruction(instruction.id).tools == before


def test_the_form_renders_the_picker(dash, store, state):
    page = dash.test_client().get(
        f"/instructions/{state['instruction'].id}/edit").get_data(as_text=True)
    assert "data-multiselect" in page
    assert 'name="tools_present"' in page
    assert page.count('name="tools"') == len(registered_tool_names())
    assert "Filter tools" in page


def test_a_new_instruction_starts_with_everything_ticked(dash):
    page = dash.test_client().get("/instructions/new").get_data(as_text=True)
    assert page.count("checked") >= len(registered_tool_names())
    assert "All tools" in page


def test_the_form_warns_that_auto_is_less_precise(dash):
    """Auto is offered, but the form must nudge toward a specific task for reliability."""
    page = dash.test_client().get("/instructions/new").get_data(as_text=True)
    assert "Auto (agent decides)" in page          # the option is there
    assert "less predictable" in page               # and so is the caution beside it
    assert "hint-warn" in page
