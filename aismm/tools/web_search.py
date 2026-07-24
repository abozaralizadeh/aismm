"""Native web-search tool (OpenAI Agents SDK hosted ``WebSearchTool``).

This is a *hosted* tool: the search runs server-side inside the Responses API, so
no local API key is needed and there is no local ``on_tool_start`` span. If your
Azure deployment / region doesn't expose the hosted web-search tool, swap this
factory for a function-tool fallback (LangChain ``{"type": "web_search"}`` on the
Responses API, or Tavily / DuckDuckGo) — the registry makes that a one-file change.
"""
from __future__ import annotations

from agents import WebSearchTool

from .registry import register_tool


def _make_web_search(state: dict):
    return WebSearchTool(search_context_size="high")


register_tool("web_search", _make_web_search)
