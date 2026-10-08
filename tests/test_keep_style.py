"""The "Keep the same style on every run" switch.

Reported as "the style is forcing the same style in every instruction from the creation
day; some we want different every time, and then it should not be passed to the model".
Off means no earlier look reaches the next run: the Video style is not sent or forced,
and the agent is told to make a new look and not to keep one in memory. On is how every
instruction worked before, and stays the default.
"""
import asyncio
import dataclasses
import json

import pytest

from aismm import video_style
from aismm.agent import prompts
from aismm.models import Instruction

PINNED = "Round cream puppy with a teal scarf. Soft watercolour."
LINE = 'Pupu says, "Look, a rainbow!"'


def _invoke(tool, **kwargs):
    from agents import RunConfig
    from agents.tool_context import ToolContext

    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="1",
                      tool_arguments="{}", run_config=RunConfig())
    out = asyncio.run(tool.on_invoke_tool(ctx, json.dumps(kwargs)))
    return json.loads(out) if isinstance(out, str) else out


def _caps():
    from aismm.platforms.base import Capabilities
    return Capabilities(supports_text=True, supports_image=True, supports_video=True,
                        needs_public_media_url=False, default_orientation="portrait",
                        caption_limit=2200)


def _account():
    from aismm.models import Account, PlatformName
    return Account(platform=PlatformName.instagram, handle="h")


# --- the rule ---------------------------------------------------------------------- #

def test_on_by_default_so_nothing_changes_for_existing_instructions():
    assert Instruction(name="I").keep_style is True
    assert video_style.keeps_style(Instruction(name="I"))


def test_off_means_a_pinned_style_is_not_forced():
    state = {"instruction": Instruction(name="I", video_style=PINNED, keep_style=False)}
    assert video_style.resolve_style(state, "fresh look") == ("fresh look", "agent", "")
    assert video_style.active_style(state["instruction"]) == ""


def test_off_keeps_the_stored_text_for_when_it_is_switched_back_on():
    instr = Instruction(name="I", video_style=PINNED, keep_style=False)
    assert video_style.pinned_style(instr) == PINNED
    assert video_style.form_value(instr) == (PINNED, True)


@pytest.mark.parametrize("builder", [prompts.build_kickoff, prompts.build_auto_kickoff])
def test_the_kickoff_asks_for_a_new_look_and_never_shows_the_old_one(builder):
    kickoff = builder(account=_account(), platform_caps=_caps(),
                      instruction=Instruction(name="I", brief="b", video_style=PINNED,
                                              last_video_style="last time's look",
                                              keep_style=False))
    assert "DIFFERENT look on every run" in kickoff
    assert "do not save the style in memory" in kickoff
    assert PINNED not in kickoff and "last time's look" not in kickoff
    assert "VIDEO STYLE" not in kickoff


@pytest.mark.parametrize("builder", [prompts.build_kickoff, prompts.build_auto_kickoff])
def test_on_is_unchanged(builder):
    pinned = builder(account=_account(), platform_caps=_caps(),
                     instruction=Instruction(name="I", brief="b", video_style=PINNED))
    plain = builder(account=_account(), platform_caps=_caps(),
                    instruction=Instruction(name="I", brief="b"))
    assert PINNED in pinned and "DIFFERENT look" not in pinned
    assert "VIDEO STYLE" not in plain and "DIFFERENT look" not in plain


def test_the_new_text_follows_the_no_em_dash_rule():
    assert "—" not in video_style.VARIED_BLOCK


# --- both video tools obey it ------------------------------------------------------- #

def test_create_video_sequence_uses_the_agents_new_style_when_off(store, monkeypatch):
    from aismm.tools import sequence_tool

    store.init()
    instr = store.upsert_instruction(Instruction(name="I", video_style=PINNED,
                                                 keep_style=False))
    seen = {}

    async def fake_perform(state, scenes, *, style, **_kw):
        seen["style"] = style
        return {"asset_path": "x.mp4"}

    monkeypatch.setattr(sequence_tool, "perform_create_sequence", fake_perform)
    monkeypatch.setattr(sequence_tool.sora_config, "enabled", lambda: True)
    tool = sequence_tool._make_create_sequence({"instruction": instr, "store": store})
    out = _invoke(tool, scenes=[LINE, "b"], style="a brand new look")
    assert seen["style"] == "a brand new look"
    assert out["style_source"] == "agent" and "style_note" not in out
    assert store.get_instruction(instr.id).video_style == PINNED      # kept, not used


def test_generate_video_does_not_prefix_the_pinned_style_when_off(monkeypatch):
    from aismm.tools import video_tool

    seen = {}

    async def fake_perform(state, prompt, **_kw):
        seen["prompt"] = prompt
        return {"asset_path": "x.mp4"}

    monkeypatch.setattr(video_tool, "perform_generate_video", fake_perform)
    monkeypatch.setattr(video_tool.sora_config, "enabled", lambda: True)
    tool = video_tool._make_generate_video(
        {"instruction": Instruction(name="I", video_style=PINNED, keep_style=False)})
    _invoke(tool, prompt=f"The puppy waves. {LINE}")
    assert PINNED not in seen["prompt"]


# --- storage ------------------------------------------------------------------------ #

def _both_backends():
    from aismm.store.azure_store import AzureStore
    from aismm.store.local_store import LocalStore
    from tests.test_azure_store import FakeTableClient

    return [("local", LocalStore(db_url="sqlite:///:memory:")),
            ("azure", AzureStore(table_client=FakeTableClient()))]


@pytest.mark.parametrize("label,backend", _both_backends(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_the_switch_round_trips(label, backend):
    backend.init()
    instr = backend.upsert_instruction(Instruction(name="I", keep_style=False))
    assert backend.get_instruction(instr.id).keep_style is False


def test_an_azure_row_saved_before_the_switch_existed_keeps_its_style():
    from aismm.store.azure_store import AzureStore
    from tests.test_azure_store import FakeTableClient

    store = AzureStore(table_client=FakeTableClient())
    store.init()
    instr = store.upsert_instruction(Instruction(name="I"))
    entity = {**store._instruction_to_entity(instr), "RowKey": instr.id}
    entity.pop("keep_style")
    assert store._instruction_from_entity(entity).keep_style is True


def test_a_sqlite_db_from_before_the_switch_gets_it_switched_on(tmp_path):
    import sqlite3

    from aismm.store.local_store import LocalStore

    db = tmp_path / "old.db"
    LocalStore(db_url=f"sqlite:///{db}").init()
    store = LocalStore(db_url=f"sqlite:///{db}")
    instr = store.upsert_instruction(Instruction(name="I"))
    with sqlite3.connect(db) as conn:                      # as if saved by the old build
        conn.execute('ALTER TABLE instruction DROP COLUMN "keep_style"')
    LocalStore(db_url=f"sqlite:///{db}").init()           # boot of the new build
    assert LocalStore(db_url=f"sqlite:///{db}").get_instruction(instr.id).keep_style is True


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
    data = {"id": instr.id, "name": instr.name, "brief": "b", "publish_mode": "dry_run",
            "media_pref": "auto", "task_type": "publish"}
    data.update(extra)
    return data


def _box(page):
    start = page.index('name="keep_style"')
    return page[page.rindex("<input", 0, start):page.index(">", start)]


def _style_label(page):
    end = page.index('name="video_style"')
    return page[page.rindex("<label", 0, end):end]


def test_new_instructions_start_with_the_box_ticked(client):
    page = client.get("/instructions/new").get_data(as_text=True)
    assert "checked" in _box(page)
    assert "hidden" not in _style_label(page)


def test_off_unticks_the_box_and_hides_the_video_style(client, store):
    instr = store.upsert_instruction(Instruction(name="I", brief="b", video_style=PINNED,
                                                 keep_style=False))
    page = client.get(f"/instructions/{instr.id}/edit").get_data(as_text=True)
    assert "checked" not in _box(page)
    assert "hidden" in _style_label(page) and "data-keep-style-only" in _style_label(page)
    assert "keeps your Video style text for later" in page


def test_unticking_saves_off_and_keeps_the_text(client, store):
    instr = store.upsert_instruction(Instruction(name="I", brief="b", video_style=PINNED))
    client.post("/instructions", data=_form(
        instr, style_fields_present="1", video_style=PINNED, video_style_shown=PINNED,
        video_style_pinned="1"))
    saved = store.get_instruction(instr.id)
    assert (saved.keep_style, saved.video_style) == (False, PINNED)


def test_ticking_saves_on(client, store):
    instr = store.upsert_instruction(Instruction(name="I", brief="b", keep_style=False))
    client.post("/instructions", data=_form(instr, style_fields_present="1", keep_style="on"))
    assert store.get_instruction(instr.id).keep_style is True


def test_a_post_without_the_field_leaves_it_alone(client, store):
    instr = store.upsert_instruction(Instruction(name="I", brief="b", keep_style=False))
    client.post("/instructions", data=_form(instr))
    assert store.get_instruction(instr.id).keep_style is False


def test_the_switch_is_hidden_for_tasks_that_post_nothing(client, store):
    instr = store.upsert_instruction(Instruction(name="I", brief="b", task_type="engage"))
    page = client.get(f"/instructions/{instr.id}/edit").get_data(as_text=True)
    wrapper = page[page.rindex("<div", 0, page.index('name="keep_style"')):
                   page.index('name="keep_style"')]
    assert "data-publish-only" in wrapper and "hidden" in wrapper
