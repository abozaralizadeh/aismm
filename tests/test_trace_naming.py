"""One LangSmith trace per run, named after the instruction.

Every trace arrived as the SDK's default "Agent workflow", so a list of them said
nothing about which instruction, account or run each one was — and diagnosing a
three-day video outage meant opening traces one by one to find the right one.
"""
from aismm.agent.manager_agent import _trace_metadata, _trace_name
from aismm.models import Account, Instruction, InstructionTask, PlatformName, Run


def _instruction(name="Kids bedtime series"):
    return Instruction(id="i-1", name=name, brief="…", workspace_id="w-1")


def _account():
    return Account(id="a-1", platform=PlatformName.youtube, handle="@snailtales",
                   external_id="UC123")


def _run():
    return Run(id="r-1", instruction_id="i-1", account_id="a-1")


def test_the_trace_is_named_after_the_instruction():
    assert _trace_name(_instruction()) == "Kids bedtime series"


def test_an_unnamed_instruction_still_gets_a_name():
    """A blank workflow name is worse than a generic one — the SDK rejects it."""
    assert _trace_name(_instruction(name="")).strip()
    assert _trace_name(_instruction(name="   ")).strip()


def test_the_metadata_leads_back_to_the_row_that_produced_the_trace():
    meta = _trace_metadata(_instruction(), _account(), _run(), InstructionTask.publish)
    assert meta["instruction_id"] == "i-1"
    assert meta["run_id"] == "r-1"
    assert meta["workspace_id"] == "w-1"
    assert meta["account"] == "@snailtales"
    assert meta["platform"] == "youtube"
    assert meta["task"] == "publish"


def test_an_account_with_no_handle_falls_back_to_its_id():
    """Several platforms never give one back; an empty label identifies nothing."""
    account = _account()
    account.handle = ""
    assert _trace_metadata(_instruction(), account, _run(),
                           InstructionTask.engage)["account"] == "UC123"


def test_every_metadata_value_is_a_string_or_a_number():
    """LangSmith stores this as JSON; an enum member would serialize as a repr."""
    meta = _trace_metadata(_instruction(), _account(), _run(), InstructionTask.auto)
    assert all(isinstance(v, (str, int, float)) for v in meta.values()), meta
