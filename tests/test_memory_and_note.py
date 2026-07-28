"""Carry-over memory + operator note: storage, tools, prompt injection, compaction.

The behaviour under test is the whole point of the feature — a scheduled run must
CONTINUE from the recorded position instead of re-running the brief from scratch.
"""
import asyncio
import dataclasses

import pytest

from aismm import config as config_module
from aismm.agent import memory as memory_module
from aismm.agent.prompts import build_kickoff
from aismm.models import Account, Instruction, MediaPref, PlatformName
from aismm.platforms.base import Capabilities
from aismm.tools import memory_tool

CAPS = Capabilities(supports_text=True, supports_image=True, supports_video=True,
                    needs_public_media_url=False, default_orientation="portrait",
                    caption_limit=2200)


@pytest.fixture()
def instruction(store):
    instr = Instruction(name="News crawl", brief="Start at 2026-03-01 and work forward.",
                        media_pref=MediaPref.auto)
    return store.upsert_instruction(instr)


@pytest.fixture()
def account():
    return Account(platform=PlatformName.instagram, handle="tester", external_id="1")


def _read(store, instruction, account):
    state = {"store": store, "instruction": instruction, "account": account}
    return asyncio.run(memory_tool.perform_read_memory(state)), state


def _write(state, **kwargs):
    return asyncio.run(memory_tool.perform_update_memory(state, **kwargs))


# --- store ---------------------------------------------------------------------- #

def test_state_starts_empty_and_is_not_persisted_until_written(store, instruction):
    state = store.get_state(instruction.id)
    assert state.memory == "" and state.note == ""
    assert state.memory_updated_at is None


def test_memory_and_note_round_trip(store, instruction):
    store.set_memory(instruction.id, "CURRENT POSITION: 2026-03-04")
    store.set_note(instruction.id, "Prefer sources from the last 48h.")
    state = store.get_state(instruction.id)
    assert state.memory == "CURRENT POSITION: 2026-03-04"
    assert state.note == "Prefer sources from the last 48h."
    assert state.memory_updated_at and state.note_updated_at


def test_setting_memory_does_not_clobber_the_note(store, instruction):
    store.set_note(instruction.id, "keep me")
    store.set_memory(instruction.id, "position")
    assert store.get_state(instruction.id).note == "keep me"


def test_compaction_counter_only_moves_for_compactions(store, instruction):
    store.set_memory(instruction.id, "a")
    assert store.get_state(instruction.id).compactions == 0
    store.set_memory(instruction.id, "b", compacted=True)
    assert store.get_state(instruction.id).compactions == 1


def test_deleting_an_instruction_drops_its_state(store, instruction):
    store.set_memory(instruction.id, "position")
    store.delete_instruction(instruction.id)
    assert store.get_state(instruction.id).memory == ""


# --- tools ---------------------------------------------------------------------- #

def test_read_memory_exposes_memory_and_note(store, instruction, account):
    store.set_memory(instruction.id, "CURRENT POSITION: 2026-03-04")
    store.set_note(instruction.id, "Be more current.")
    result, _ = _read(store, instruction, account)
    assert "2026-03-04" in result["memory"]
    assert result["operator_note"] == "Be more current."


def test_read_memory_says_so_on_the_first_run(store, instruction, account):
    result, _ = _read(store, instruction, account)
    assert "first run" in result["memory"]
    assert result["operator_note"] == "(none)"


def test_update_memory_replaces_by_default(store, instruction, account):
    _, state = _read(store, instruction, account)
    _write(state, memory="first")
    _write(state, memory="second")
    assert store.get_state(instruction.id).memory == "second"
    assert state["memory_written"] is True


def test_update_memory_can_append_with_a_timestamp(store, instruction, account):
    _, state = _read(store, instruction, account)
    _write(state, memory="first")
    _write(state, memory="second", append=True)
    saved = store.get_state(instruction.id).memory
    assert "first" in saved and "second" in saved and "UTC]" in saved


def test_update_memory_rejects_empty(store, instruction, account):
    _, state = _read(store, instruction, account)
    result = _write(state, memory="   ")
    assert result["error"] == "empty_memory"


def test_update_memory_keeps_the_tail_when_oversized(store, instruction, account):
    """Truncation must keep the NEWEST text — that's where the position lives."""
    _, state = _read(store, instruction, account)
    huge = "x" * (memory_tool.MAX_MEMORY_CHARS + 500) + "POSITION: the end"
    result = _write(state, memory=huge)
    assert result["truncated"] is True
    assert store.get_state(instruction.id).memory.endswith("POSITION: the end")


# --- prompt injection ------------------------------------------------------------ #

def test_kickoff_carries_the_memory_forward(store, instruction, account):
    store.set_memory(instruction.id, "CURRENT POSITION: covered up to 2026-03-14.")
    kickoff = build_kickoff(account=account, instruction=instruction, platform_caps=CAPS,
                            state=store.get_state(instruction.id))
    assert "covered up to 2026-03-14" in kickoff
    assert "do not restart" in kickoff


def test_kickoff_marks_a_first_run(store, instruction, account):
    kickoff = build_kickoff(account=account, instruction=instruction, platform_caps=CAPS,
                            state=store.get_state(instruction.id))
    assert "this is the first run" in kickoff


def test_kickoff_carries_the_operator_note_as_an_override(store, instruction, account):
    store.set_note(instruction.id, "Search for more up-to-date content.")
    kickoff = build_kickoff(account=account, instruction=instruction, platform_caps=CAPS,
                            state=store.get_state(instruction.id))
    assert "Search for more up-to-date content." in kickoff
    assert "OVERRIDES" in kickoff


def test_kickoff_omits_the_note_section_when_there_is_none(store, instruction, account):
    kickoff = build_kickoff(account=account, instruction=instruction, platform_caps=CAPS,
                            state=store.get_state(instruction.id))
    assert "OPERATOR NOTE" not in kickoff


def test_kickoff_works_without_any_state():
    """Callers that pass no state (older code paths) must not break."""
    kickoff = build_kickoff(account=Account(platform=PlatformName.twitter),
                            instruction=Instruction(name="x", brief="b"), platform_caps=CAPS)
    assert "BRIEF" in kickoff


# --- compaction ------------------------------------------------------------------ #

def _with_limit(monkeypatch, limit):
    monkeypatch.setattr(memory_module, "settings",
                        dataclasses.replace(config_module.settings, memory_max_chars=limit))


def test_small_memory_is_left_alone(store, instruction, monkeypatch):
    _with_limit(monkeypatch, 1000)
    store.set_memory(instruction.id, "short")
    monkeypatch.setattr(memory_module, "compact_memory",
                        lambda m: (_ for _ in ()).throw(AssertionError("should not compact")))
    assert asyncio.run(memory_module.maybe_compact(instruction.id, store)) is False


def test_oversized_memory_is_summarized(store, instruction, monkeypatch):
    _with_limit(monkeypatch, 100)
    store.set_memory(instruction.id, "y" * 500)

    async def fake_compact(memory):
        return "CURRENT POSITION: 2026-03-14\nNEXT STEP: 2026-03-15"

    monkeypatch.setattr(memory_module, "compact_memory", fake_compact)
    assert asyncio.run(memory_module.maybe_compact(instruction.id, store)) is True
    state = store.get_state(instruction.id)
    assert state.memory.startswith("CURRENT POSITION")
    assert state.compactions == 1


def test_failed_compaction_keeps_the_original_memory(store, instruction, monkeypatch):
    """A summarizer outage must never destroy the agent's position."""
    _with_limit(monkeypatch, 100)
    original = "z" * 500
    store.set_memory(instruction.id, original)

    async def boom(memory):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(memory_module, "compact_memory", boom)
    assert asyncio.run(memory_module.maybe_compact(instruction.id, store)) is False
    assert store.get_state(instruction.id).memory == original


def test_compaction_that_saves_nothing_is_discarded(store, instruction, monkeypatch):
    _with_limit(monkeypatch, 100)
    original = "z" * 500
    store.set_memory(instruction.id, original)

    async def longer(memory):
        return memory + " and more"

    monkeypatch.setattr(memory_module, "compact_memory", longer)
    assert asyncio.run(memory_module.maybe_compact(instruction.id, store)) is False
    assert store.get_state(instruction.id).memory == original


# --- dashboard round-trip --------------------------------------------------------- #

def test_note_and_memory_survive_a_dashboard_save(store, instruction, monkeypatch, tmp_path):
    """The operator must be able to add a note without wiping the agent's memory."""
    from aismm.dashboard import app as app_module

    monkeypatch.setattr(app_module, "get_store", lambda: store)
    app = app_module.create_app()
    app.secret_key = "test"
    store.set_memory(instruction.id, "CURRENT POSITION: 2026-03-14")

    client = app.test_client()
    client.post("/instructions", data={
        "id": instruction.id, "name": instruction.name, "brief": instruction.brief,
        "schedule": "", "publish_mode": "dry_run", "media_pref": "auto",
        "note": "Search for more up-to-date content.",
        "memory": "CURRENT POSITION: 2026-03-14",
    })

    state = store.get_state(instruction.id)
    assert state.note == "Search for more up-to-date content."
    assert state.memory == "CURRENT POSITION: 2026-03-14"


def test_operator_can_reset_the_memory_from_the_dashboard(store, instruction, monkeypatch):
    from aismm.dashboard import app as app_module

    monkeypatch.setattr(app_module, "get_store", lambda: store)
    app = app_module.create_app()
    app.secret_key = "test"
    store.set_memory(instruction.id, "wrong position")

    app.test_client().post("/instructions", data={
        "id": instruction.id, "name": instruction.name, "brief": instruction.brief,
        "schedule": "", "publish_mode": "dry_run", "media_pref": "auto",
        "note": "", "memory": "",
    })
    assert store.get_state(instruction.id).memory == ""


# --- runs page ------------------------------------------------------------------- #

def test_runs_page_links_each_run_to_its_instruction(store, instruction, monkeypatch):
    """A run is meaningless without knowing which instruction produced it."""
    from aismm.dashboard import app as app_module
    from aismm.models import Account, PlatformName, Run

    account = store.upsert_account(Account(platform=PlatformName.instagram, handle="demo",
                                           external_id="1"), access_token="t")
    store.add_run(Run(instruction_id=instruction.id, account_id=account.id))
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    app = app_module.create_app()
    app.secret_key = "test"

    page = app.test_client().get("/runs").get_data(as_text=True)
    assert instruction.name in page
    assert f"/instructions/{instruction.id}/edit" in page      # clickable
    assert "instagram" in page and "demo" in page              # which account too


def test_runs_page_survives_a_deleted_instruction(store, monkeypatch):
    from aismm.dashboard import app as app_module
    from aismm.models import Run

    store.add_run(Run(instruction_id="gone-forever", account_id="also-gone"))
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    app = app_module.create_app()
    app.secret_key = "test"

    response = app.test_client().get("/runs")
    assert response.status_code == 200
    assert "deleted instruction" in response.get_data(as_text=True)
