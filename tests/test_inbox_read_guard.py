"""An engage run cannot end claiming it checked an inbox it never opened.

Reported, twice, with the DM tools present and no error in the log:

    Engagement done: 0 replied, 0 staged, 0 skipped. Read comments across 12
    recent posts/reels, all recent mentions, and inbound DMs; no comments or
    DMs needed replies…

That sentence is model-written prose. What was actually READ is now recorded in
code by `engagement.note_read`, and `finish_engagement` compares the two — the
same reasoning as the publish ledger and the AI disclosure: a guarantee that must
hold on every path cannot live in a claim the model makes about itself.
"""
import asyncio

import pytest

from aismm import engagement
from aismm.models import Account, Instruction, InstructionTask, PlatformName, Run, RunStatus
from aismm.tools import engagement_finish
from aismm.tools.engagement_finish import perform_finish_engagement, unread_inboxes


def _state(store, *, tools, read=(), **extra):
    account = Account(platform=PlatformName.instagram, handle="genaicomicbook",
                      external_id="178414")
    store.upsert_account(account, access_token="t")
    instruction = Instruction(name="Comicbook Comments", brief="b",
                              task_type=InstructionTask.engage)
    store.upsert_instruction(instruction)
    run = Run(instruction_id=instruction.id, account_id=account.id,
              status=RunStatus.running)
    store.add_run(run)
    return {"account": account, "instruction": instruction, "store": store, "run": run,
            "tool_names": set(tools), "read_tools_used": set(read), **extra}


def _finish(state, summary="done"):
    return asyncio.run(perform_finish_engagement(state, summary))


# --- the guard ---------------------------------------------------------------------------- #

def test_finishing_without_reading_the_dms_is_refused(store):
    state = _state(store, tools=["instagram_dms", "instagram_recent_comments"])
    result = _finish(state)
    assert result["error"] == "inbox_not_read"
    assert "instagram_dms" in result["message"]
    assert store.get_run(state["run"].id).status is RunStatus.running   # not closed


def test_finishing_after_reading_them_is_allowed(store):
    state = _state(store, tools=["instagram_dms"], read=["instagram_dms"])
    assert "error" not in _finish(state)


def test_a_run_with_no_dm_tool_is_not_nagged(store):
    """Nothing to have looked at — the earlier bug was the opposite case."""
    state = _state(store, tools=["instagram_recent_comments"])
    assert "error" not in _finish(state)


def test_the_refusal_names_what_to_do_next(store):
    state = _state(store, tools=["instagram_dms"])
    message = _finish(state)["message"]
    assert "Call instagram_dms" in message
    assert "having looked" in message


@pytest.mark.parametrize("tool,what", [("instagram_dms", "inbound Instagram DMs"),
                                       ("x_dms", "inbound X DMs"),
                                       ("reddit_dms", "inbound Reddit messages")])
def test_every_dm_platform_is_covered(store, tool, what):
    state = _state(store, tools=[tool])
    assert unread_inboxes(state) == [what]


# --- but it must not be able to wedge the run --------------------------------------------- #

def test_a_run_that_will_not_look_still_ends(store):
    """Burning the whole budget on this exchange would leave no record at all."""
    state = _state(store, tools=["instagram_dms"])
    for _ in range(engagement_finish._MAX_NUDGES):
        assert _finish(state)["error"] == "inbox_not_read"
    result = _finish(state)
    assert "error" not in result


def test_and_it_ends_honestly(store):
    state = _state(store, tools=["instagram_dms"])
    for _ in range(engagement_finish._MAX_NUDGES):
        _finish(state)
    _finish(state, "Nothing needed a reply.")
    assert "NOT CHECKED this run: inbound Instagram DMs" in store.get_run(
        state["run"].id).log or ""


# --- what records the read ----------------------------------------------------------------- #

def test_note_read_records_the_tool():
    state = {}
    engagement.note_read(state, "instagram_dms")
    assert state["read_tools_used"] == {"instagram_dms"}


def test_the_dm_tool_records_that_it_ran(monkeypatch, store):
    """The recording has to happen where the read happens, or the guard is blind."""
    from aismm.tools import instagram_tools

    account = Account(platform=PlatformName.instagram, handle="ig", external_id="1")
    state = {"account": account, "instruction": Instruction(name="e", brief="b",
                                                            task_type=InstructionTask.engage),
             "store": store, "run": None}

    class _Platform:
        async def list_dms(self, token, acct, *, limit=25):
            return []

    async def context(_state):
        return _Platform(), account, "token"

    monkeypatch.setattr(instagram_tools, "_instagram_context", context)
    from agents import RunConfig
    from agents.tool_context import ToolContext

    tool = instagram_tools._make_dms(state)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="1",
                      tool_arguments="{}", run_config=RunConfig())
    asyncio.run(tool.on_invoke_tool(ctx, '{"limit": 25}'))
    assert "instagram_dms" in state.get("read_tools_used", set())
