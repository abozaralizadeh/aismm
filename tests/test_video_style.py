"""The video STYLE is on the instruction, where the operator can see and change it.

Reported as "Style should not be hidden from user, should be accessible from the
instruction". The style is repeated verbatim in every shot's prompt, yet it existed only
in the agent's tool calls and memory. That's where "no voice, no narrator, no dialogue"
survived on the children's channel after its brief was fixed, and where a psychologist
reel picked up "no music cues" nobody had asked for. Pinned here:

* a Video style set on the instruction IS the style: both video tools use it verbatim
  and the agent's own is ignored (and the agent is told so);
* the style actually used is recorded on the instruction for the operator to see,
  without clobbering edits made while the video rendered, and never fatally;
* the agent sees a pinned style in its first turn;
* the dashboard saves the field, and a POST without it does not wipe it;
* both storage backends keep both fields.
"""
import asyncio
import dataclasses
import json

import pytest

from aismm import video_style
from aismm.agent import prompts
from aismm.models import Instruction

PINNED = "Round cream puppy with a teal scarf. Soft watercolour. Gentle ukulele music."


def _invoke(tool, **kwargs):
    from agents import RunConfig
    from agents.tool_context import ToolContext

    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="1",
                      tool_arguments="{}", run_config=RunConfig())
    out = asyncio.run(tool.on_invoke_tool(ctx, json.dumps(kwargs)))
    return json.loads(out) if isinstance(out, str) else out


# --- the rule ----------------------------------------------------------------------- #

def test_a_pinned_style_wins_and_the_agent_is_told():
    state = {"instruction": Instruction(name="I", video_style=f"  {PINNED}  ")}
    style, source, note = video_style.resolve_style(state, "Looks only: no voice.")
    assert (style, source) == (PINNED, "instruction")
    assert "was not used" in note


def test_no_note_when_the_agent_passed_nothing_or_the_same_style():
    state = {"instruction": Instruction(name="I", video_style=PINNED)}
    assert video_style.resolve_style(state, "")[2] == ""
    assert video_style.resolve_style(state, PINNED)[2] == ""


def test_without_a_pinned_style_the_agent_writes_it():
    state = {"instruction": Instruction(name="I")}
    assert video_style.resolve_style(state, " mine ") == ("mine", "agent", "")


# --- recording what was used ------------------------------------------------------- #

def test_the_used_style_is_recorded_without_clobbering_a_concurrent_edit(store):
    store.init()
    instr = store.upsert_instruction(Instruction(name="I", brief="old brief"))
    run_copy = store.get_instruction(instr.id)          # the copy the run holds

    edited = store.get_instruction(instr.id)            # operator edits mid-run
    edited.brief = "new brief"
    store.upsert_instruction(edited)

    video_style.record_used_style({"instruction": run_copy, "store": store}, "agent style")
    got = store.get_instruction(instr.id)
    assert got.last_video_style == "agent style"
    assert got.brief == "new brief", "writing the run's stale copy back would undo the edit"


def test_recording_never_fails_a_video():
    class Broken:
        def get_instruction(self, _id):
            raise RuntimeError("storage down")

    video_style.record_used_style(
        {"instruction": Instruction(name="I"), "store": Broken()}, "x")   # must not raise


# --- the agent sees it -------------------------------------------------------------- #

def _caps():
    from aismm.platforms.base import Capabilities
    return Capabilities(supports_text=True, supports_image=True, supports_video=True,
                        needs_public_media_url=False, default_orientation="portrait",
                        caption_limit=2200)


def _account():
    from aismm.models import Account, PlatformName
    return Account(platform=PlatformName.youtube, handle="kids")


@pytest.mark.parametrize("builder", [prompts.build_kickoff, prompts.build_auto_kickoff])
def test_the_kickoff_shows_a_pinned_style(builder):
    pinned = builder(account=_account(), instruction=Instruction(name="I", brief="b",
                                                                  video_style=PINNED),
                     platform_caps=_caps())
    assert "VIDEO STYLE" in pinned and PINNED in pinned
    plain = builder(account=_account(), instruction=Instruction(name="I", brief="b"),
                    platform_caps=_caps())
    assert "VIDEO STYLE" not in plain


# --- both tools use it ---------------------------------------------------------------- #

def test_create_video_sequence_uses_the_pinned_style(store, monkeypatch):
    from aismm.tools import sequence_tool

    store.init()
    instr = store.upsert_instruction(Instruction(name="I", video_style=PINNED))
    seen = {}

    async def fake_perform(state, scenes, *, style, **_kw):
        seen["style"] = style
        return {"asset_path": "x.mp4"}

    monkeypatch.setattr(sequence_tool, "perform_create_sequence", fake_perform)
    monkeypatch.setattr(sequence_tool.sora_config, "enabled", lambda: True)
    state = {"instruction": instr, "store": store}
    tool = sequence_tool._make_create_sequence(state)
    out = _invoke(tool, scenes=["a", "b"], style="Looks only: no voice, no narrator.")

    assert seen["style"] == PINNED
    assert out["style_source"] == "instruction" and "was not used" in out["style_note"]
    assert store.get_instruction(instr.id).last_video_style == PINNED


def test_create_video_sequence_records_the_agents_own_style(store, monkeypatch):
    from aismm.tools import sequence_tool

    store.init()
    instr = store.upsert_instruction(Instruction(name="I"))

    async def fake_perform(state, scenes, *, style, **_kw):
        return {"asset_path": "x.mp4"}

    monkeypatch.setattr(sequence_tool, "perform_create_sequence", fake_perform)
    monkeypatch.setattr(sequence_tool.sora_config, "enabled", lambda: True)
    tool = sequence_tool._make_create_sequence({"instruction": instr, "store": store})
    out = _invoke(tool, scenes=["a"], style="agent's own look")

    assert out["style_source"] == "agent" and "style_note" not in out
    assert store.get_instruction(instr.id).last_video_style == "agent's own look"


def test_a_failed_video_records_nothing(store, monkeypatch):
    from aismm.tools import sequence_tool

    store.init()
    instr = store.upsert_instruction(Instruction(name="I"))

    async def fake_perform(state, scenes, *, style, **_kw):
        return {"error": "sora_failed", "message": "no"}

    monkeypatch.setattr(sequence_tool, "perform_create_sequence", fake_perform)
    monkeypatch.setattr(sequence_tool.sora_config, "enabled", lambda: True)
    tool = sequence_tool._make_create_sequence({"instruction": instr, "store": store})
    _invoke(tool, scenes=["a"], style="never rendered")
    assert store.get_instruction(instr.id).last_video_style == ""


def test_generate_video_prefixes_the_pinned_style(monkeypatch):
    from aismm.tools import video_tool

    seen = {}

    async def fake_perform(state, prompt, **_kw):
        seen["prompt"] = prompt
        return {"asset_path": "x.mp4"}

    monkeypatch.setattr(video_tool, "perform_generate_video", fake_perform)
    monkeypatch.setattr(video_tool.sora_config, "enabled", lambda: True)
    tool = video_tool._make_generate_video(
        {"instruction": Instruction(name="I", video_style=PINNED)})
    out = _invoke(tool, prompt="The puppy waves.")
    assert seen["prompt"].startswith(f"STYLE: {PINNED}")
    assert seen["prompt"].endswith("The puppy waves.")
    assert out["style_source"] == "instruction"


# --- storage ------------------------------------------------------------------------ #

def _both_backends():
    from aismm.store.azure_store import AzureStore
    from aismm.store.local_store import LocalStore
    from tests.test_azure_store import FakeTableClient

    return [("local", LocalStore(db_url="sqlite:///:memory:")),
            ("azure", AzureStore(table_client=FakeTableClient()))]


@pytest.mark.parametrize("label,backend", _both_backends(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_both_fields_round_trip(label, backend):
    backend.init()
    instr = backend.upsert_instruction(
        Instruction(name="I", video_style=PINNED, last_video_style="used"))
    got = backend.get_instruction(instr.id)
    assert (got.video_style, got.last_video_style) == (PINNED, "used")


# --- the dashboard ------------------------------------------------------------------ #

@pytest.fixture()
def client(store, monkeypatch, tmp_path):
    from aismm import assets as assets_module
    from aismm import config as config_module
    from aismm.config import AuthSettings
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    (tmp_path / "assets").mkdir(exist_ok=True)
    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module, assets_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    store.init()
    application = app_module.create_app()
    application.secret_key = "test"
    return application.test_client()


def _form(instr, **extra):
    data = {"id": instr.id, "name": instr.name, "brief": instr.brief or "b", "schedule": "",
            "publish_mode": "dry_run", "media_pref": "auto", "task_type": "publish"}
    data.update(extra)
    return data


def test_the_form_shows_the_field_and_the_last_style_used(client, store):
    instr = store.upsert_instruction(Instruction(name="Kids", brief="b",
                                                 last_video_style="Looks only: no voice."))
    page = client.get(f"/instructions/{instr.id}/edit").get_data(as_text=True)
    assert 'name="video_style"' in page
    assert "Style used in the last video" in page and "Looks only: no voice." in page
    assert "data-copy-style" in page


def test_saving_the_form_stores_the_video_style(client, store):
    instr = store.upsert_instruction(Instruction(name="Kids", brief="b"))
    client.post("/instructions", data=_form(instr, video_style=f"{PINNED}\r\n"))
    assert store.get_instruction(instr.id).video_style == PINNED


def test_a_post_without_the_field_keeps_the_pinned_style(client, store):
    instr = store.upsert_instruction(Instruction(name="Kids", brief="b", video_style=PINNED))
    client.post("/instructions", data=_form(instr))
    assert store.get_instruction(instr.id).video_style == PINNED
