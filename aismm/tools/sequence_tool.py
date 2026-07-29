"""``plan_video`` / ``create_video_sequence`` — long videos from short clips.

Sora 2 renders 4, 8 or 12 second clips. A one-minute post is therefore several
clips stitched together, and the two hard parts are *deciding the shape* and
*keeping the clips looking like one video*.

**The agent decides the length.** ``plan_video(target_seconds)`` turns "make me
60 seconds" into a concrete segment plan using only the durations Sora accepts,
and reports the exact total so the agent can adjust before spending anything.
Nothing is hard-coded to a house format.

**Consistency: what GenBox does, and what this does differently.**
GenBox picks ONE lever per shot type — a fresh create with a fixed "bible" for the
first clip of a speaker, remix for later clips of that speaker, last-frame
chaining for face-free b-roll — and drift still shows. Three reasons, each
addressed here:

1. *The style description only appeared in the first clip's prompt.* Later clips
   described the new action and trusted the reference to carry the look. Here the
   ``style`` block is repeated **verbatim in every clip's prompt**, which is the
   cheapest and most reliable lever Sora gives you.
2. *A reference frame was passed without telling the model what to do with it.*
   Sora treats ``input_reference`` as a loose starting point. Here the prompt
   explicitly says the supplied frame is the previous shot's final frame and the
   clip must continue from it — the instruction and the image reinforce each other.
3. *Retries rotated to another Sora resource.* A remix only exists on the resource
   that made its base clip, so once GenBox's ``_safe_create`` moved a clip to a new
   endpoint the visual identity was gone. Here the **whole sequence is pinned to
   the resource that served clip 1**, so remix stays available throughout.

On top of that, ``continuity="auto"`` falls back from frame chaining to **remixing
clip 1** when a reference is refused — Azure's Sora rejects ``input_reference``
containing human faces, which is precisely when GenBox loses continuity. A fresh
unrelated clip is never silently substituted.

Sora 2 has **no seed**, so none of this makes clips identical — it makes them
plausibly the same scene. That limit is the model's, not the code's.
"""
from __future__ import annotations

import asyncio
import logging

from agents import function_tool

from .. import video
from ..assets import public_url, save_bytes
from . import sora_config
from .registry import register_tool
from .sora_client import create_clip, create_clip_with_failover, format_http_error, remix_clip

logger = logging.getLogger("aismm.tools.sequence")

ALLOWED_SECONDS = (4, 8, 12)
MAX_CLIPS = 12                      # 12 × 12s = 144s, well past any platform's limit
_FACE_REJECTION_MARKERS = ("input_reference", "face", "person", "people", "human")


def plan_segments(target_seconds: int, prefer: int = 12) -> dict:
    """Split a target duration into clips of Sora's allowed lengths.

    Uses the largest allowed clip as the base (fewer clips = fewer seams and less
    drift), then makes the remainder up from the smaller sizes. Returns the plan
    plus the achievable total, which may differ from the request — 30s cannot be
    hit exactly with 4/8/12, and silently returning 28 or 32 without saying so
    would be worse than saying which.
    """
    target = max(int(target_seconds or 0), 0)
    if target <= 0:
        return {"segments": [], "total_seconds": 0, "clip_count": 0,
                "note": "target_seconds must be positive"}

    base = prefer if prefer in ALLOWED_SECONDS else 12
    if target <= min(ALLOWED_SECONDS):
        return {"segments": [min(ALLOWED_SECONDS)], "total_seconds": min(ALLOWED_SECONDS),
                "clip_count": 1,
                "note": f"minimum clip length is {min(ALLOWED_SECONDS)}s"}

    segments = [base] * (target // base)
    remainder = target - sum(segments)
    if remainder:
        # Fill the remainder with the smallest clip that covers it.
        fill = next((s for s in sorted(ALLOWED_SECONDS) if s >= remainder), base)
        segments.append(fill)

    note = ""
    if len(segments) > MAX_CLIPS:
        segments = segments[:MAX_CLIPS]
        note = (f"capped at {MAX_CLIPS} clips ({sum(segments)}s); ask for a shorter "
                f"video or longer clips")
    total = sum(segments)
    if total != target and not note:
        note = (f"{target}s is not reachable with {'/'.join(map(str, ALLOWED_SECONDS))}s "
                f"clips; this plan is {total}s")
    return {"segments": segments, "total_seconds": total, "clip_count": len(segments),
            "note": note}


def build_clip_prompt(scene: str, style: str, *, index: int, total: int,
                      continues_from_frame: bool) -> str:
    """Assemble one clip's prompt: style + continuity contract + this scene.

    The style block is repeated in EVERY clip — that is lever 1. When a reference
    frame is attached, the prompt says so explicitly — that is lever 2.
    """
    parts = []
    if style.strip():
        parts.append(f"STYLE (keep identical in every shot): {style.strip()}")
    if continues_from_frame:
        parts.append(
            "CONTINUITY: the supplied reference image is the FINAL FRAME of the "
            "previous shot. Begin this shot from that exact framing, lighting, "
            "subject and wardrobe, then perform the action below. Do not restyle, "
            "recolour, relight or reframe the scene, and do not cut to a new location."
        )
    elif index > 1:
        parts.append(
            "CONTINUITY: this is a later shot of the SAME scene and subject as the "
            "previous shot — identical look, lighting and wardrobe. Change only the "
            "action described below."
        )
    parts.append(f"SHOT {index} of {total}: {scene.strip()}")
    parts.append("No on-screen text, captions, subtitles, logos or watermarks.")
    return "\n".join(parts)


def _looks_like_reference_rejection(detail: str) -> bool:
    low = (detail or "").lower()
    return any(marker in low for marker in _FACE_REJECTION_MARKERS)


async def perform_create_sequence(
    state: dict, scenes: list[str], *, style: str = "", seconds_each: int = 8,
    orientation: str = "portrait", continuity: str = "auto",
    scene_seconds: list[int] | None = None,
) -> dict:
    """Generate each scene with continuity, then merge into one MP4."""
    scenes = [s for s in (scenes or []) if (s or "").strip()][:MAX_CLIPS]
    if not scenes:
        return {"error": "no_scenes", "message": "Pass at least one scene description."}
    if not video.ffmpeg_available():
        return {"error": "ffmpeg_missing",
                "message": "Video merging needs imageio-ffmpeg (pip install -r requirements.txt)."}

    size = (sora_config.SIZE_PORTRAIT if orientation.lower().startswith("p")
            else sora_config.SIZE_LANDSCAPE)
    lengths = list(scene_seconds or [])
    while len(lengths) < len(scenes):
        lengths.append(seconds_each)
    lengths = [sora_config.normalize_seconds(s) for s in lengths[:len(scenes)]]
    mode = (continuity or "auto").lower()

    clips: list[bytes] = []
    details: list[dict] = []
    resource = None            # pinned after clip 1 so remix stays available
    base_job_id = ""
    reference: bytes | None = None

    for index, (scene, seconds) in enumerate(zip(scenes, lengths), start=1):
        use_frame = mode in {"auto", "frame"} and reference is not None
        prompt = build_clip_prompt(scene, style, index=index, total=len(scenes),
                                   continues_from_frame=use_frame)
        clip = job_id = None
        how = ""

        # A later clip in remix mode reuses clip 1's job — the strongest lever.
        if index > 1 and mode == "remix" and resource is not None and base_job_id:
            try:
                clip, job_id = await remix_clip(resource, base_job_id, prompt)
                how = "remix"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Remix of %s failed (%s); creating instead", base_job_id, exc)

        if clip is None:
            try:
                if resource is None:
                    clip, job_id, resource = await create_clip_with_failover(
                        prompt, seconds, size, ref_image_bytes=reference if use_frame else None)
                    how = "create"
                else:
                    # Stay on the pinned resource so remix remains possible.
                    clip, job_id = await create_clip(
                        resource, prompt, seconds, size,
                        reference if use_frame else None)
                    how = "create+frame" if use_frame else "create"
            except Exception as exc:  # noqa: BLE001
                detail = format_http_error(exc) if hasattr(exc, "response") else str(exc)
                # Azure refuses input_reference containing faces — exactly where
                # GenBox loses continuity. Fall back to remixing clip 1 rather
                # than making an unrelated clip.
                if (use_frame and mode == "auto" and base_job_id and resource is not None
                        and _looks_like_reference_rejection(detail)):
                    logger.info("Reference refused for shot %d (%s); remixing clip 1 instead",
                                index, detail[:160])
                    try:
                        retry_prompt = build_clip_prompt(
                            scene, style, index=index, total=len(scenes),
                            continues_from_frame=False)
                        clip, job_id = await remix_clip(resource, base_job_id, retry_prompt)
                        how = "remix(fallback)"
                    except Exception as remix_exc:  # noqa: BLE001
                        logger.warning("Remix fallback failed too: %s", remix_exc)
                if clip is None:
                    logger.error("Shot %d/%d failed: %s", index, len(scenes), detail[:300])
                    if not clips:
                        return {"error": "video_generation_failed",
                                "message": f"First shot failed: {detail}"}
                    break        # keep what we have rather than losing everything

        clips.append(clip)
        if index == 1:
            base_job_id = job_id or ""
        details.append({"shot": index, "seconds": seconds, "how": how, "job_id": job_id})
        logger.info("Shot %d/%d done (%s, %ss, %d bytes)",
                    index, len(scenes), how, seconds, len(clip))

        if mode in {"auto", "frame"}:
            try:
                reference = await asyncio.to_thread(video.extract_last_frame, clip, size)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Last-frame extraction failed for shot %d: %s", index, exc)
                reference = None

    try:
        merged = await asyncio.to_thread(video.concat_clips, clips, size)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Merging failed")
        return {"error": "merge_failed", "message": str(exc)}

    path = save_bytes(merged, "mp4")
    duration = await asyncio.to_thread(video.duration_seconds, merged)
    asset = {"path": path, "kind": "video", "public_url": public_url(path),
             "size": size, "seconds": round(duration, 1), "shots": details}
    state.setdefault("assets", []).append(asset)
    result = {"asset_path": path, "public_url": asset["public_url"], "kind": "video",
              "size": size, "duration_seconds": round(duration, 1),
              "clips_merged": len(clips), "shots": details}
    if len(clips) < len(scenes):
        result["warning"] = (f"only {len(clips)} of {len(scenes)} shots rendered; "
                             f"the video is shorter than planned")
    return result


def _make_plan_video(state: dict):
    @function_tool
    async def plan_video(target_seconds: int, prefer_clip_seconds: int = 12) -> dict:
        """Work out how to build a video of a given length from Sora clips.

        Sora renders only 4, 8 or 12 second clips, so anything longer is several
        clips merged. Call this first when the brief asks for a duration ("a
        one-minute reel"), then pass one scene description per segment to
        ``create_video_sequence``.

        Args:
            target_seconds: How long the finished video should be.
            prefer_clip_seconds: Base clip length — 12 gives the fewest seams and
                the least visual drift; 4 gives finer control over pacing.

        Returns the segment lengths, the clip count, and the total actually
        achievable — check ``note``, since not every duration is reachable exactly.
        """
        return plan_segments(target_seconds, prefer_clip_seconds)

    return plan_video


def _make_create_sequence(state: dict):
    if not sora_config.enabled():
        return None

    @function_tool
    async def create_video_sequence(
        scenes: list[str],
        style: str = "",
        seconds_each: int = 8,
        orientation: str = "portrait",
        continuity: str = "auto",
        scene_seconds: list[int] | None = None,
    ) -> dict:
        """Generate several Sora clips that look like one scene, and merge them.

        Use this for any video longer than 12 seconds. Describe each shot
        separately, in order; the shots are rendered with continuity between them
        and stitched into a single MP4 you pass to ``publish``.

        Args:
            scenes: One description per shot, in order (max 12). Describe only what
                CHANGES in each — the shared look belongs in ``style``.
            style: The look to hold constant across every shot: subject, wardrobe,
                location, lighting, lens, mood. This text is repeated verbatim in
                every shot's prompt, which is the single most effective thing you
                can do for consistency. Be specific and reuse it unchanged.
            seconds_each: Clip length for every shot (snapped to 4, 8 or 12).
            orientation: "portrait" for Reels/TikTok/Shorts, "landscape" otherwise.
            continuity: "auto" (recommended) chains each shot from the previous
                shot's final frame and falls back to remixing shot 1 if the
                reference is refused — which happens when human faces are in
                frame. "frame" chains only. "remix" derives every later shot from
                shot 1, the strongest lever when people are on camera. "none" makes
                independent clips.
            scene_seconds: Optional per-shot lengths, overriding ``seconds_each``.

        Returns the merged ``asset_path``, its real duration, and per-shot detail
        showing how each was produced. Sora 2 has no seed, so shots will be
        similar rather than identical — keep ``style`` rich and shot changes small.
        """
        if state.get("video_failures", 0) >= 2:
            return {"error": "video_circuit_open",
                    "message": "Video generation failed repeatedly this run."}
        result = await perform_create_sequence(
            state, scenes, style=style, seconds_each=seconds_each,
            orientation=orientation, continuity=continuity, scene_seconds=scene_seconds)
        if result.get("error"):
            state["video_failures"] = state.get("video_failures", 0) + 1
        return result

    return create_video_sequence


register_tool("plan_video", _make_plan_video)
register_tool("create_video_sequence", _make_create_sequence)
