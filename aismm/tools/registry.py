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


def build_tools(state: dict) -> list[Any]:
    """Instantiate all registered tools for one run's ``state``."""
    tools: list[Any] = []
    for factory in _TOOL_FACTORIES.values():
        tool = factory(state)
        if tool is not None:
            tools.append(tool)
    return tools
