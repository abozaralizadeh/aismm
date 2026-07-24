"""``generate_video`` — Sora 2 video generation as an agent tool.

Deterministic work only (no LLM calls inside the tool, per the SandBox rule): it
renders a clip, saves it to the assets dir, records it on the run ``state``, and
returns a reference the agent then passes to ``publish``. Disables itself (factory
returns ``None``) when no Sora resource is configured.
"""
from __future__ import annotations

import logging

from agents import function_tool

from ..assets import public_url, save_bytes
from . import sora_config
from .registry import register_tool
from .sora_client import generate_video_bytes

logger = logging.getLogger("aismm.tools.video")

# Circuit breaker: stop hammering Sora if it keeps failing within one run.
_MAX_VIDEO_FAILURES = 2


def _make_generate_video(state: dict):
    if not sora_config.enabled():
        return None

    @function_tool
    async def generate_video(prompt: str, seconds: int = 8, orientation: str = "portrait") -> dict:
        """Generate a short social video clip with Sora 2.

        Args:
            prompt: Vivid description of the clip (scene, subject, motion, mood).
                Do NOT ask for on-screen text/captions/logos — add those as the
                post caption instead.
            seconds: Desired clip length; snapped to Sora's 4, 8, or 12s.
            orientation: "portrait" (9:16 — Reels/TikTok/Shorts) or "landscape"
                (16:9 — YouTube/X). Choose to match the target platform.

        Returns a dict with ``asset_path`` and ``public_url`` to pass to ``publish``.
        """
        if state.get("video_failures", 0) >= _MAX_VIDEO_FAILURES:
            return {"error": "video_circuit_open",
                    "message": "Video generation failed repeatedly this run; proceed without video."}
        size = sora_config.SIZE_PORTRAIT if orientation.lower().startswith("p") else sora_config.SIZE_LANDSCAPE
        secs = sora_config.normalize_seconds(seconds)
        try:
            mp4 = await generate_video_bytes(prompt, secs, size)
        except Exception as exc:  # noqa: BLE001
            state["video_failures"] = state.get("video_failures", 0) + 1
            logger.warning("generate_video failed: %s", exc)
            return {"error": "video_generation_failed", "message": str(exc)}
        path = save_bytes(mp4, "mp4")
        asset = {"path": path, "kind": "video", "public_url": public_url(path),
                 "seconds": secs, "size": size}
        state.setdefault("assets", []).append(asset)
        return {"asset_path": path, "public_url": asset["public_url"],
                "kind": "video", "seconds": secs, "size": size}

    return generate_video


register_tool("generate_video", _make_generate_video)
