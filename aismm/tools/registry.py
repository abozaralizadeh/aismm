"""Extensible tool registry.

A *tool factory* is ``fn(state: dict) -> Tool | None``. Registering one makes it
available to the manager agent on every run. Return ``None`` from a factory to
opt out for a given run (e.g. the Sora tool disables itself when no Sora resource
is configured).

Add a new capability without touching the agent::

    from agents import function_tool
    from aismm.tools.registry import register_tool

    def _make_my_tool(state):
        @function_tool
        async def my_tool(arg: str) -> dict:
            '''What the model sees.'''
            ...
        return my_tool

    register_tool("my_tool", _make_my_tool)
"""
from __future__ import annotations

from typing import Any, Callable

ToolFactory = Callable[[dict], Any]

_TOOL_FACTORIES: dict[str, ToolFactory] = {}


def register_tool(name: str, factory: ToolFactory) -> None:
    _TOOL_FACTORIES[name] = factory


def registered_tool_names() -> list[str]:
    return list(_TOOL_FACTORIES)


# Ending a run needs a terminal tool, so it is never withheld however the
# selection is set — an instruction that switched its terminal off could never
# finish. Which terminal depends on the run's job: a PUBLISH run ends with
# ``publish``, an ENGAGE run with ``finish_engagement``; ``report_failure`` is
# the shared "I couldn't do it" ending for both. The wrong terminal is worse than
# none (an engage run offered ``publish`` will publish a post it was never asked
# to), so the sets are disjoint and picked by task type.
ALWAYS_ON_PUBLISH = ("publish", "report_failure")
# ENGAGE (answer my comments) and OUTREACH (engage others' content) share this
# terminal set: both end by replying/liking or doing nothing, never by posting.
ALWAYS_ON_ENGAGE = ("finish_engagement", "report_failure")
# An AUTO run decides publish-vs-engage itself, so it keeps BOTH terminals — this
# is the one case where the disjoint-set rule above is deliberately relaxed,
# because the operator asked the agent to choose. It equals the union.
ALWAYS_ON_AUTO = ("publish", "finish_engagement", "report_failure")
# The union is what every non-terminal-selection check should treat as "always
# there regardless of the tool picker".
ALWAYS_ON = ALWAYS_ON_AUTO


def always_on_for(task_type) -> tuple[str, ...]:
    """The terminal tools for a run of this ``InstructionTask`` (accepts the enum
    or its ``.value``)."""
    value = getattr(task_type, "value", task_type)
    if value in ("engage", "outreach"):
        return ALWAYS_ON_ENGAGE
    if value == "auto":
        return ALWAYS_ON_AUTO
    return ALWAYS_ON_PUBLISH


def build_tools(state: dict, enabled: list[str] | None = None) -> list[Any]:
    """Instantiate registered tools for one run's ``state``.

    ``enabled`` is the instruction's tool selection: ``None`` (or empty) means
    every registered tool, which is the default. Narrowing it is a way to keep an
    instruction on task — a text-only account has no use for Sora — and to cut the
    number of choices a smaller model has to reason about.

    The run's terminal tool set is chosen from the instruction's ``task_type``
    (``state["instruction"]``): a publish run never sees ``finish_engagement`` and
    an engage run never sees ``publish``, so the model cannot end the wrong way.
    The OTHER task's terminals are withheld outright even if the picker names them.

    A factory may still return ``None`` for its own reasons (Sora with no resource
    configured, the Instagram tools on a non-Instagram run); that is unaffected.
    """
    wanted = set(enabled or ())
    instruction = state.get("instruction")
    keep = always_on_for(getattr(instruction, "task_type", None))
    drop = set(ALWAYS_ON) - set(keep)   # the other task's terminals — never build them
    tools: list[Any] = []
    for name, factory in _TOOL_FACTORIES.items():
        if name in drop:
            continue
        if wanted and name not in wanted and name not in keep:
            continue
        tool = factory(state)
        if tool is not None:
            tools.append(tool)
    return tools
