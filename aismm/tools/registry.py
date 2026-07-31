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


# Ending a run needs one of these; an instruction that switched them both off
# could never finish, so they are never withheld however the selection is set.
ALWAYS_ON = ("publish", "report_failure")


def build_tools(state: dict, enabled: list[str] | None = None) -> list[Any]:
    """Instantiate registered tools for one run's ``state``.

    ``enabled`` is the instruction's tool selection: ``None`` (or empty) means
    every registered tool, which is the default. Narrowing it is a way to keep an
    instruction on task — a text-only account has no use for Sora — and to cut the
    number of choices a smaller model has to reason about.

    A factory may still return ``None`` for its own reasons (Sora with no resource
    configured, the Instagram tools on a non-Instagram run); that is unaffected.
    """
    wanted = set(enabled or ())
    tools: list[Any] = []
    for name, factory in _TOOL_FACTORIES.items():
        if wanted and name not in wanted and name not in ALWAYS_ON:
            continue
        tool = factory(state)
        if tool is not None:
            tools.append(tool)
    return tools
