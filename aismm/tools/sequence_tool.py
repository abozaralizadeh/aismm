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
the previous shot** when a reference is refused — Azure's Sora rejects
``input_reference`` containing human faces, which is precisely when GenBox loses
continuity. A fresh unrelated clip is never silently substituted.

**Remix chains from the PREVIOUS shot, never from shot 1.** Anchoring every
remix to shot 1 was the original design, and it produced a video whose first
moment played three times over: each later shot applied its own prompt to the
same untouched starting point, so nothing ever advanced. Chaining means shot 3
remixes shot 2, which remixes shot 1 — the action moves forward, and the repeated
``style`` block is what holds the look together. Drift across a chain is the
lesser evil; literal repetition is not a video.

**A remix inherits the source clip's duration** (the API takes only a prompt), so
a shot asking for 8s that falls back to remixing a 4s clip *renders 4s*. Every
clip is therefore measured after the fact and the real per-shot duration is
reported — a plan of 4/4/4/8 that silently returned 16s instead of 20s is how
this was found.

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
from .sora_client import (
    create_clip, create_clip_with_failover, format_http_error, load_reference_image,
    looks_like_reference_rejection, remix_clip,
)

logger = logging.getLogger("aismm.tools.sequence")

ALLOWED_SECONDS = (4, 8, 12)
MAX_CLIPS = 12                      # 12 × 12s = 144s, well past any platform's limit
# Stop only when the failures look systemic (a dead resource, no credits) rather
# than incidental. Below this a failed shot is skipped and the sequence goes on.
_MAX_SHOT_FAILURES = 3
# Moved to sora_client so generate_video can use it too.


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
                      continues_from_frame: bool, continues_from_remix: bool = False,
                      is_cut: bool = False, from_supplied_image: bool = False) -> str:
    """Assemble one clip's prompt: style + continuity contract + this scene.

    The style block is repeated in EVERY clip — that is lever 1. When a reference
    frame is attached, the prompt says so explicitly — that is lever 2. A remix
    gets its own wording (lever 3): the source video the model is editing IS the
    previous shot, and saying so is what turns "another take of the same moment"
    into "the next moment".

    Two more contracts, because "continue from the last shot" is not always the
    right instruction:

    * ``from_supplied_image`` — the reference is a picture the operator chose,
      NOT the previous shot's tail. Telling the model to "continue from" it makes
      it try to resume an action that never happened; it should treat it as the
      look and the subject to open on.
    * ``is_cut`` — a deliberate cut to a new angle or moment. Without saying so,
      a later shot handed continuity language produces another take of the same
      beat, which is what reads as *repeats*. Saying "new shot, same film" is what
      makes the sequence move.
    """
    parts = []
    if style.strip():
        parts.append(f"STYLE (keep identical in every shot): {style.strip()}")
    if from_supplied_image:
        parts.append(
            "SOURCE IMAGE: the supplied reference is a still of THIS story's "
            "subject and setting. Match its characters, wardrobe, colours and art "
            "style exactly, and animate the action described below from it. It is "
            "a reference for how things LOOK, not a frame to resume — do not treat "
            "it as a paused video."
        )
    elif is_cut:
        parts.append(
            "CUT: this is a NEW shot in the same film — a different angle, subject "
            "or moment. Keep the style, world, characters and wardrobe identical, "
            "but do NOT continue the previous shot's action or framing. Show what "
            "is described below as its own moment."
        )
    elif continues_from_frame:
        parts.append(
            "CONTINUITY: the supplied reference image is the FINAL FRAME of the "
            "previous shot. Begin this shot from that exact framing, lighting, "
            "subject and wardrobe, then perform the action below. Do not restyle, "
            "recolour, relight or reframe the scene, and do not cut to a new location."
        )
    elif continues_from_remix:
        parts.append(
            "CONTINUITY: the video you are editing is the PREVIOUS shot of this "
            "sequence. Keep its subject, wardrobe, location, lighting and framing "
            "exactly, and ADVANCE the action to what is described below — this is "
            "the NEXT moment, not another take of the same one. Do not restart the "
            "scene or repeat the previous action."
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
    return looks_like_reference_rejection(detail)


async def perform_create_sequence(
    state: dict, scenes: list[str], *, style: str = "", seconds_each: int = 12,
    orientation: str = "portrait", continuity: str = "auto",
    scene_seconds: list[int] | None = None, reference_asset_path: str = "",
    reference_asset_paths: list[str] | None = None,
    scene_continuity: list[str] | None = None,
) -> dict:
    """Generate each scene, then merge into one MP4.

    Three per-shot controls, because one setting for the whole sequence is what
    produced gaps and repeats: ``scene_seconds`` (how long), ``scene_continuity``
    (continue or cut), and ``reference_asset_paths`` (which picture anchors it).
    """
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

    # Per-shot continuity. "" inherits the sequence mode; "cut" makes this shot a
    # deliberate new angle. A sequence that continues from every shot has no
    # cuts, which is why a trailer built that way felt like one long take with
    # repeats in it.
    per_shot_mode = list(scene_continuity or [])
    while len(per_shot_mode) < len(scenes):
        per_shot_mode.append("")
    per_shot_mode = [(m or "").strip().lower() for m in per_shot_mode[:len(scenes)]]

    # Per-shot reference images. Shot i is anchored to the picture the agent
    # chose for it; one seed doing the work for a whole sequence is how a
    # character nobody described drifted into somebody else entirely.
    supplied = list(reference_asset_paths or [])
    if reference_asset_path and not supplied:
        supplied = [reference_asset_path]
    while len(supplied) < len(scenes):
        supplied.append("")
    supplied = supplied[:len(scenes)]

    seeds: list[bytes | None] = []
    seed_notes: list[str] = []
    for path in supplied:
        data, note = load_reference_image(path, size)
        if path and data is None:
            logger.warning("Reference %s unusable: %s", path, note)
            seed_notes.append(f"{path}: {note}")
        seeds.append(data)

    clips: list[bytes] = []
    details: list[dict] = []
    resource = None            # pinned after clip 1 so remix stays available
    previous_job_id = ""       # the shot just rendered — what a remix chains FROM

    reference: bytes | None = None      # the previous shot's final frame
    refused_seeds: list[int] = []
    failed_shots: list[dict] = []

    for index, (scene, seconds) in enumerate(zip(scenes, lengths), start=1):
        shot_mode = per_shot_mode[index - 1] or mode
        is_cut = shot_mode in {"cut", "none"} and index > 1
        seed = seeds[index - 1]

        # A picture the agent chose for THIS shot wins over the chained frame:
        # naming that panel is a more specific instruction than "continue from
        # the last shot", and the shared style block still carries the look.
        if seed is not None:
            active, from_image = seed, True
        elif is_cut:
            active, from_image = None, False
        else:
            active, from_image = (reference if mode in {"auto", "frame"} else None), False

        use_frame = active is not None
        prompt = build_clip_prompt(scene, style, index=index, total=len(scenes),
                                   continues_from_frame=use_frame and not from_image,
                                   is_cut=is_cut, from_supplied_image=from_image)
        clip = job_id = None
        how = ""

        # A later clip in remix mode derives from the PREVIOUS shot, so the action
        # advances. Remixing shot 1 every time replays shot 1 every time. A shot
        # with its own reference image, or a deliberate cut, is never remixed.
        if (index > 1 and shot_mode == "remix" and seed is None and not is_cut
                and resource is not None and previous_job_id):
            try:
                remix_prompt = build_clip_prompt(
                    scene, style, index=index, total=len(scenes),
                    continues_from_frame=False, continues_from_remix=True)
                clip, job_id = await remix_clip(resource, previous_job_id, remix_prompt)
                how = "remix"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Remix of %s failed (%s); creating instead", previous_job_id, exc)

        if clip is None:
            try:
                if resource is None:
                    clip, job_id, resource = await create_clip_with_failover(
                        prompt, seconds, size, ref_image_bytes=active)
                    how = "create+image" if from_image else ("create+frame" if use_frame
                                                             else "create")
                else:
                    # Stay on the pinned resource so remix remains possible.
                    clip, job_id = await create_clip(resource, prompt, seconds, size, active)
                    how = "create+image" if from_image else ("create+frame" if use_frame
                                                             else "create")
            except Exception as exc:  # noqa: BLE001
                detail = format_http_error(exc) if hasattr(exc, "response") else str(exc)
                # A refused SUPPLIED image is retried without it: the operator
                # picked that panel for this shot, so remixing the previous shot
                # would silently answer a different request. The style block still
                # carries the look.
                if from_image and _looks_like_reference_rejection(detail):
                    logger.info("Reference image refused for shot %d (%s); rendering it "
                                "from the prompt alone", index, detail[:160])
                    refused_seeds.append(index)
                    retry_prompt = build_clip_prompt(
                        scene, style, index=index, total=len(scenes),
                        continues_from_frame=False, is_cut=is_cut)
                    try:
                        if resource is None:
                            clip, job_id, resource = await create_clip_with_failover(
                                retry_prompt, seconds, size)
                        else:
                            clip, job_id = await create_clip(
                                resource, retry_prompt, seconds, size, None)
                        how = "create(image refused)"
                    except Exception as retry_exc:  # noqa: BLE001
                        logger.warning("Shot %d failed without its image too: %s",
                                       index, retry_exc)
                # Azure refuses input_reference containing faces — exactly where
                # GenBox loses continuity. Fall back to remixing the PREVIOUS shot
                # rather than making an unrelated clip (or replaying shot 1).
                elif (use_frame and mode == "auto" and previous_job_id and resource is not None
                        and _looks_like_reference_rejection(detail)):
                    logger.info("Reference refused for shot %d (%s); remixing shot %d (%s) "
                                "instead", index, detail[:160], index - 1, previous_job_id)
                    try:
                        retry_prompt = build_clip_prompt(
                            scene, style, index=index, total=len(scenes),
                            continues_from_frame=False, continues_from_remix=True)
                        clip, job_id = await remix_clip(resource, previous_job_id, retry_prompt)
                        how = "remix(fallback)"
                    except Exception as remix_exc:  # noqa: BLE001
                        logger.warning("Remix fallback failed too: %s", remix_exc)
                if clip is None:
                    # Skip this shot and keep going. Abandoning the rest on the
                    # first failure turned one transient Sora error into a
                    # 12-second stub of a nine-shot trailer — eight shots that
                    # were never even attempted. A sequence is independent
                    # clips; one bad clip is a gap, not the end.
                    logger.error("Shot %d/%d failed, continuing with the rest: %s",
                                 index, len(scenes), detail[:300])
                    failed_shots.append({"shot": index, "error": detail[:300]})
                    if len(failed_shots) >= _MAX_SHOT_FAILURES:
                        logger.error("Giving up after %d failed shots", len(failed_shots))
                        break
                    continue

        clips.append(clip)
        previous_job_id = job_id or previous_job_id

        # Measure what was ACTUALLY rendered. A remix takes only a prompt and
        # inherits its source's duration, so a shot that asked for 8s and fell
        # back to remixing a 4s clip is 4s — reporting the request would be a lie,
        # and the agent writes its caption from these numbers.
        try:
            actual = round(await asyncio.to_thread(video.duration_seconds, clip), 1)
        except Exception as exc:  # noqa: BLE001 - never fail a run over measurement
            logger.warning("Could not measure shot %d: %s", index, exc)
            actual = float(seconds)
        detail_row = {"shot": index, "seconds": actual, "how": how, "job_id": job_id}
        if abs(actual - seconds) >= 1:
            detail_row["requested_seconds"] = seconds
        details.append(detail_row)
        logger.info("Shot %d/%d done (%s, %ss%s, %d bytes)", index, len(scenes), how, actual,
                    f" — {seconds}s requested" if "requested_seconds" in detail_row else "",
                    len(clip))

        # Keep the tail frame for whichever later shot wants to continue from it.
        if mode in {"auto", "frame"} or any(m == "" for m in per_shot_mode[index:]):
            try:
                reference = await asyncio.to_thread(video.extract_last_frame, clip, size)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Last-frame extraction failed for shot %d: %s", index, exc)
                reference = None

    if not clips:
        first = failed_shots[0]["error"] if failed_shots else "no clips were produced"
        return {"error": "video_generation_failed",
                "message": f"Every shot failed. First error: {first}"}

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
    asked_for = sum(1 for path in supplied if path)
    if asked_for:
        used = sum(1 for row in details if row.get("how", "").startswith("create+image"))
        result["reference_images_used"] = used
        result["reference_images_given"] = asked_for
        notes = list(seed_notes)
        if refused_seeds:
            notes.append(
                f"Sora refused the reference image on shot(s) "
                f"{', '.join(str(i) for i in refused_seeds)} — it rejects images "
                f"containing human faces. Those shots came from the prompt and style "
                f"alone, so describe the character IN `style` to keep them consistent.")
        if notes:
            result["reference_notes"] = notes

    warnings = []
    if failed_shots:
        result["failed_shots"] = failed_shots
    if len(clips) < len(scenes):
        which = ", ".join(str(row["shot"]) for row in failed_shots)
        warnings.append(f"only {len(clips)} of {len(scenes)} shots rendered "
                        f"({'shot(s) ' + which + ' failed' if which else 'some failed'}); "
                        f"the video is shorter than planned. The clips that DID render "
                        f"are merged and usable — publish them if the result still tells "
                        f"the story, or report_failure if it does not")
    # A remix cannot honour a requested length, so say so rather than letting the
    # agent describe a duration the file does not have.
    short = [d for d in details if "requested_seconds" in d]
    if short:
        requested = sum(d.get("requested_seconds", d["seconds"]) for d in details)
        warnings.append(
            f"{len(short)} shot(s) did not render at the requested length "
            f"(a remix inherits its source clip's duration), so this video is "
            f"{round(duration, 1)}s, not {requested}s. Describe the REAL duration, or "
            f"re-run with continuity=\"none\" if the exact length matters more than "
            f"the visual match.")
    if warnings:
        result["warning"] = " ".join(warnings)
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
            prefer_clip_seconds: Base clip length. **Leave this at 12** unless you
                have a reason: 12s clips give the fewest seams, the least visual
                drift and room for the action to breathe. Short clips are a tool
                for specific beats, not a default — a video built from 4s clips is
                mostly cuts, and each clip has barely time to move.

        Returns the segment lengths, the clip count, and the total actually
        achievable — check ``note``, since not every duration is reachable exactly.
        The plan is a STARTING POINT: vary the real lengths per shot with
        ``scene_seconds`` so the pacing matches the story rather than a grid.
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
        seconds_each: int = 12,
        orientation: str = "portrait",
        continuity: str = "auto",
        scene_seconds: list[int] | None = None,
        reference_asset_path: str = "",
        reference_asset_paths: list[str] | None = None,
        scene_continuity: list[str] | None = None,
    ) -> dict:
        """Generate several Sora clips and merge them into one video.

        Use this for any video longer than 12 seconds. YOU direct it: how long
        each shot runs, which image anchors it, and whether it continues the
        previous shot or cuts to a new one. Those are per-shot decisions — one
        setting applied to a whole sequence is what makes a video feel like one
        long take with repeats in it.

        Args:
            scenes: One description per shot, in order (max 12). Describe only
                what CHANGES in each — the shared look belongs in ``style``. Each
                scene must be the NEXT beat, never a restatement of the last.
            style: The look to hold constant across every shot: the CHARACTERS
                (name, age, hair, eyes, build, wardrobe, distinguishing marks),
                the location, lighting, lens, palette and mood. Repeated verbatim
                in every shot's prompt, which is the single most effective thing
                you can do for consistency — and the only one that still works
                when a reference image is refused. If a character matters, they
                belong here in detail; a character nobody described is a
                character the model invents.
            seconds_each: Default clip length (snapped to 4, 8 or 12). Prefer 12:
                fewer, longer shots read as film, while many 4s shots read as a
                slideshow and give the model no room to move. Use
                ``scene_seconds`` to vary it per shot.
            orientation: "portrait" for Reels/TikTok/Shorts, "landscape" otherwise.
            continuity: The DEFAULT for shots that don't say otherwise. "auto"
                (recommended) chains each shot from the previous shot's final
                frame and falls back to remixing the previous shot if the
                reference is refused. "remix" derives each from the one before —
                the strongest lever when people are on camera. "frame" chains
                only. "none" makes every shot an independent cut.
            scene_seconds: Per-shot lengths. Match the length to the SHOT: a held
                emotional beat or an establishing shot wants 12s; a quick reaction
                or an impact wants 4s. Mixing lengths is what gives a sequence
                rhythm.
            scene_continuity: Per-shot direction, one entry per scene. "" (the
                default) uses ``continuity``. **"cut"** makes that shot a
                deliberate new angle or moment — same world, same characters, same
                style, but not a continuation. Use "cut" whenever the story moves
                to a different place, subject or time; forcing continuity across a
                jump is what produces gaps and repeated action. "remix" derives
                this shot from the previous one.
            reference_asset_paths: One image per shot, same order as ``scenes``;
                use "" for shots with no image. The real picture is sent to Sora
                as the look and subject for that shot.

                **The strongest way to fill this is to paint the opening frames
                yourself.** For shot 1 and every ``"cut"`` shot, call
                ``generate_image`` first with your character sheet in its
                ``reference_asset_paths``, describing that exact moment, and pass
                the result here. Image generation takes up to 16 references and
                none of Sora's restrictions, so it is where you actually control
                who is on screen. Leave ``""`` for shots that CONTINUE — those
                chain from the previous shot's final frame, which is the point of
                continuity. A saved panel or photo works too, as long as it shows
                what that shot is about. Never describe an image in the prompt in
                place of passing it.
            reference_asset_path: Shorthand for a single image on shot 1.

        Returns the merged ``asset_path``, its **measured** duration, and per-shot
        detail: ``how`` says whether a shot used its image ("create+image"), the
        previous frame ("create+frame"), a remix, or nothing.

        Check three things in the result. ``reference_images_used`` vs
        ``reference_images_given`` — Sora rejects images containing human faces,
        and ``reference_notes`` names the shots it refused, which are exactly the
        shots whose consistency now depends on ``style``. A remix inherits its
        source clip's duration, so a shot that fell back to remixing renders at
        the PREVIOUS shot's length and its ``requested_seconds`` will differ.
        And Sora 2 has no seed, so shots are similar rather than identical.
        """
        if state.get("video_failures", 0) >= 2:
            return {"error": "video_circuit_open",
                    "message": "Video generation failed repeatedly this run."}
        result = await perform_create_sequence(
            state, scenes, style=style, seconds_each=seconds_each,
            orientation=orientation, continuity=continuity, scene_seconds=scene_seconds,
            reference_asset_path=reference_asset_path,
            reference_asset_paths=reference_asset_paths,
            scene_continuity=scene_continuity)
        if result.get("error"):
            state["video_failures"] = state.get("video_failures", 0) + 1
        return result

    return create_video_sequence


register_tool("plan_video", _make_plan_video)
register_tool("create_video_sequence", _make_create_sequence)
