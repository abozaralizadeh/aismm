"""``get_context`` — lets the agent (re)read its brief + the target platform's
capabilities and formatting constraints at any point during a run.
"""
from __future__ import annotations

from agents import function_tool

from .registry import register_tool


def _make_get_context(state: dict):
    @function_tool
    async def get_context() -> dict:
        """Return the brief, target account/platform, and platform constraints.

        Use this to remind yourself what to post, for whom, and in what format
        (caption limits, supported media, recommended orientation).
        """
        account = state["account"]
        instruction = state["instruction"]
        from ..platforms.registry import get_platform  # lazy

        caps = get_platform(account.platform).capabilities
        return {
            "instruction_name": instruction.name,
            "brief": instruction.brief,
            "media_preference": instruction.media_pref.value,
            "account_handle": account.handle,
            "platform": account.platform.value,
            "platform_capabilities": {
                "supports_text": caps.supports_text,
                "supports_image": caps.supports_image,
                "supports_video": caps.supports_video,
                "recommended_orientation": caps.default_orientation,
                "caption_limit": caps.caption_limit,
                "notes": caps.notes,
            },
            "assets_generated": [
                {"kind": a["kind"], "asset_path": a["path"]} for a in state.get("assets", [])
            ],
        }

    return get_context


register_tool("get_context", _make_get_context)
