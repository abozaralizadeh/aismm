"""Agent tools.

Importing this package registers the built-in tool factories with the registry
(web search, Sora video, image generation, publishing). Add your own by calling
:func:`aismm.tools.registry.register_tool` — see ``registry.py``.
"""
from __future__ import annotations

# Import side effects register each tool factory.
from . import (  # noqa: F401
    browse_tool, context_tool, image_tool, memory_tool, publish_tool, video_tool, web_search,
)
from .registry import build_tools, register_tool

__all__ = ["build_tools", "register_tool"]
