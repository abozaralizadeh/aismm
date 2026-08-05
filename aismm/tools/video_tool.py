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
from .sora_client import (
    create_clip_with_failover, format_http_error, load_reference_image,
    looks_like_reference_rejection,
)

logger = logging.getLogger("aismm.tools.video")

# Circuit breaker: stop hammering Sora if it keeps failing within one run.
_MAX_VIDEO_FAILURES = 2


def _load_reference(asset_path: str, size: str):
    reference, note = load_reference_image(asset_path, size)
    if asset_path and reference is None:
        logger.warning("Reference %s unusable: %s", asset_path, note)
    return reference, note


async def perform_generate_video(state: dict, prompt: str, *, seconds: int = 8,
                                 orientation: str = "portrait",
                                 reference_asset_path: str = "") -> dict:
    """Render one clip and record it on the run state (extracted for testability)."""
    if state.get("video_failures", 0) >= _MAX_VIDEO_FAILURES:
        return {"error": "video_circuit_open",
                "message": "Video generation failed repeatedly this run; proceed without video."}
    size = (sora_config.SIZE_PORTRAIT if orientation.lower().startswith("p")
            else sora_config.SIZE_LANDSCAPE)
    secs = sora_config.normalize_seconds(seconds)

    reference, reference_note = _load_reference(reference_asset_path, size)
    used_reference = reference is not None
    try:
        # Load-balanced across the Sora pool; fails over to another resource.
        mp4, job_id, resource = await create_clip_with_failover(
            prompt, secs, size, ref_image_bytes=reference)
    except Exception as exc:  # noqa: BLE001
        detail = format_http_error(exc) if hasattr(exc, "response") else str(exc)
        # Sora refuses an input_reference containing human faces. Losing the clip
        # entirely over that is worse than losing the reference — but the agent
        # has to be told, or it will caption a clip that never used the picture.
        if reference is not None and looks_like_reference_rejection(detail):
            logger.info("Sora refused the reference image (%s); generating from the "
                        "prompt alone", detail[:160])
            reference_note = ("Sora refused the reference image (it rejects human faces); "
                              "the clip was generated from the prompt alone.")
            used_reference = False
            try:
                mp4, job_id, resource = await create_clip_with_failover(prompt, secs, size)
            except Exception as retry_exc:  # noqa: BLE001
                state["video_failures"] = state.get("video_failures", 0) + 1
                logger.warning("generate_video failed: %s", retry_exc)
                return {"error": "video_generation_failed", "message": str(retry_exc)}
        else:
            state["video_failures"] = state.get("video_failures", 0) + 1
            logger.warning("generate_video failed: %s", exc)
            return {"error": "video_generation_failed", "message": str(exc)}

    path = save_bytes(mp4, "mp4")
    # Keep the serving endpoint + job id on the asset: a Sora job is only
    # addressable on the resource that created it (poll/download/remix).
    asset = {"path": path, "kind": "video", "public_url": public_url(path),
             "seconds": secs, "size": size,
             "sora_endpoint": resource["endpoint"], "sora_job_id": job_id}
    state.setdefault("assets", []).append(asset)
    result = {"asset_path": path, "public_url": asset["public_url"],
              "kind": "video", "seconds": secs, "size": size}
    if reference_asset_path:
        result["reference_used"] = used_reference
        if reference_note:
            result["reference_note"] = reference_note
    return result


def _make_generate_video(state: dict):
    if not sora_config.enabled():
        return None

    @function_tool
    async def generate_video(prompt: str, seconds: int = 8, orientation: str = "portrait",
                             reference_asset_path: str = "") -> dict:
        """Generate a short social video clip with Sora 2.

        Args:
            prompt: Vivid description of the clip (scene, subject, motion, mood).
                Do NOT ask for on-screen text/captions/logos — add those as the
                post caption instead.
            seconds: Desired clip length; snapped to Sora's 4, 8, or 12s.
            orientation: "portrait" (9:16 — Reels/TikTok/Shorts) or "landscape"
                (16:9 — YouTube/X). Choose to match the target platform.
            reference_asset_path: An IMAGE to build the clip from — an
                ``asset_path`` from ``save_media``, ``generate_image`` or a
                reference attachment. The real picture is sent to Sora, so use
                this whenever you want the clip to look like something you
                already have; do NOT describe the image in the prompt instead,
                which throws away everything the picture actually shows. It is
                fitted to the clip size for you.

        Returns ``asset_path`` and ``public_url`` to pass to ``publish``. When a
        reference was given, ``reference_used`` says whether Sora accepted it —
        it refuses images containing human faces, and the clip is then generated
        from the prompt alone.
        """
        return await perform_generate_video(
            state, prompt, seconds=seconds, orientation=orientation,
            reference_asset_path=reference_asset_path)

    return generate_video


register_tool("generate_video", _make_generate_video)
