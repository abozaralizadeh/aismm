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

**Sora refuses ANY ``input_reference`` showing a human face — including one that
gpt-image-2 just drew.** Who made the picture is irrelevant; if there is a face
in it, it is rejected. So for anything with people on camera, reference images
are NOT the consistency lever, and painting an opening frame to pass in is work
that gets thrown away. What is left, in order:

1. the ``style`` block, repeated verbatim in every shot — it survives everything;
2. **remix**, which is the real lever: the model edits the previous clip rather
   than making a new one, so subject, wardrobe, lighting and framing carry over.

So **remix is the default** (``continuity="remix"``). ``continuity="auto"`` tries
frame chaining once and, the moment a reference is refused, switches the REST of
the sequence to remix — the refusal is proof there are faces in this material, so
paying for the same rejection on every remaining shot is pure waste. Reference
images remain useful for material with no people in it: locations, objects,
artwork, landscapes.

**A refused image is not a reason to render a shot unanchored.** The picture is
out of play whatever we do next, so the only question left is what this shot is
tied to, and any anchor from this sequence beats none: remix an earlier clip,
else re-use a picture Sora has already accepted here, and only then fall back to
the prompt alone — reported, because that shot is where the look will have moved.
A seven-shot trailer had exactly two unanchored shots and they were exactly the
two whose cast changed.

**Pin the cast, let the story move.** The continuity clause used to demand the
source clip's "location, lighting and framing exactly" and then hand Sora a scene
that moved to a hill at twilight in the rain. A prompt that contradicts itself is
settled by regenerating, and what gets regenerated is the characters. So
``_CAST_CONTRACT`` states the invariant — the characters, their designs and the
art style — on every shot, and the continuity clause now says the framing, place,
time of day and light follow the scene.

**Remix is the chain, and its SOURCE is a per-shot decision.** Every shot after
the first edits an earlier clip of this same sequence — including cuts, where the
source fixes the look and the prompt asks for a new moment. The default source is
the shot just before, so the action advances; anchoring every remix to shot 1 was
the original design and it published a reel whose opening moment played three
times, because each shot applied its own prompt to the same untouched start.

But chaining always forward drifts a little further with every link, so
``scene_remix_from`` lets a shot name any earlier one: a return to the opening
framing, the establishing wide, or a character last seen at the start remixes
THAT shot rather than its neighbour. Forward for continuity, back for recall.

**A remix inherits the source clip's duration** (the API takes only a prompt), so
a chained sequence is uniform at ``seconds_each`` whatever ``scene_seconds`` asks
for. This is not worked around, because the workaround — rendering a shot fresh
to hit a length — throws away the only continuity lever there is. The video is
``n × seconds_each``, and pacing is fixed in the WRITING:

* ``plan_shot_timing`` measures how much of each clip the dialogue accounts for
  and flags the two failures — ``over`` (the line is cut off mid-sentence) and
  ``under`` (dead air at the end of the shot);
* the target is a band, not a number (``_FILL_FLOOR``..``_FILL_CEILING``). Writing
  to exactly the clip length is what breaks a sentence when the model delivers it
  slightly slower than the arithmetic predicts — the margin IS the feature.

Every clip is still measured afterwards and the real duration reported: a plan of
4/4/4/8 that silently returned 16s instead of 20s is how the inheritance was found.

**One clip at a time, on one resource.** The shots are rendered strictly
sequentially — shot N+1 cannot start before N finishes, because it may need to
remix it or chain from its final frame. Load balancing happens BETWEEN runs (the
pool cursor advances each time a sequence starts), never inside one: a job id
exists only on the resource that created it, so a mid-sequence hop to another
endpoint would leave remix with nothing to remix.

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
# How many consecutive forward links before the drift is worth reporting. Four
# shots chained one after another puts the last three generations from the first,
# which is where a cast visibly stops being the same cast.
_CHAIN_DRIFT_LINKS = 3
# Moved to sora_client so generate_video can use it too.


# Conversational delivery, words per second. Deliberately on the slow side: the
# failure being fixed is a clip that ends mid-sentence, and the cost of a shot
# that is half a second long at the end is nothing next to a cut-off word.
_WORDS_PER_SECOND = 2.3
# Room to breathe at each end of a line — a character does not start talking on
# frame 1 and stop on the last frame.
_LEAD_IN_SECONDS = 0.7
_TAIL_SECONDS = 1.0


def estimate_speech_seconds(text: str) -> float:
    """Roughly how long it takes to SAY ``text``, with lead-in and tail."""
    words = len((text or "").split())
    if not words:
        return 0.0
    return _LEAD_IN_SECONDS + words / _WORDS_PER_SECOND + _TAIL_SECONDS


def fit_seconds(needed: float) -> int:
    """Smallest allowed clip length that fits ``needed`` — always rounding UP.

    Rounding DOWN is precisely the reported bug: a line that needs 9 seconds put
    in an 8-second clip is a clip that ends mid-word. Past the longest clip the
    answer is the longest clip, and the caller is told to split the line instead.
    """
    for allowed in sorted(ALLOWED_SECONDS):
        if needed <= allowed:
            return allowed
    return max(ALLOWED_SECONDS)


# How full a clip's dialogue should make it. Below the floor the clip has dead
# air at the end; above the ceiling the delivery only has to run slightly slow for
# the last words to be cut off. The gap between them is the risk margin.
_FILL_FLOOR = 0.55
_FILL_CEILING = 0.85


def plan_shot_lengths(shots: list[dict] | None, seconds_each: int = 12) -> dict:
    """Check that each shot has enough scenario to FILL its clip, and not too much.

    The reported failures were clips cut mid-sentence and clips with nothing
    happening at the end. Both are the same mistake seen from two sides: the clip
    length is fixed by Sora, so it is the WRITING that has to match it.

    Every clip is ``seconds_each`` long — a remixed chain cannot vary its length
    anyway — so this does not choose lengths. It reports, per shot, how much of
    the clip the dialogue actually accounts for and what to do about it:

    * ``under`` — dead air. Give the shot more: another line, a reaction, an
      action beat, a camera move.
    * ``ok`` — inside the margin. The words end before the clip does, with room
      for a slower delivery than you expect.
    * ``over`` — the line will be cut off. Move some of it into the next shot.

    The margin is the point. Writing to exactly the clip length is what breaks a
    sentence when the model delivers it a little slower than the arithmetic says.
    """
    rows = list(shots or [])
    seconds_each = sora_config.normalize_seconds(seconds_each or max(ALLOWED_SECONDS))
    if not rows:
        return {"shots": [], "scene_seconds": [], "total_seconds": 0, "clip_count": 0,
                "seconds_each": seconds_each, "note": "pass one entry per shot"}

    out: list[dict] = []
    notes: list[str] = []
    for position, row in enumerate(rows[:MAX_CLIPS], start=1):
        row = row if isinstance(row, dict) else {"action": str(row)}
        dialogue = str(row.get("dialogue") or "")
        action = str(row.get("action") or "")

        speech = estimate_speech_seconds(dialogue)
        fill = speech / seconds_each if seconds_each else 0.0
        if speech and fill > _FILL_CEILING:
            verdict = "over"
            notes.append(
                f"shot {position}: about {round(speech, 1)}s of dialogue in a {seconds_each}s "
                f"clip leaves no margin — it will be cut off mid-sentence. Move roughly "
                f"{round(speech - seconds_each * _FILL_CEILING, 1)}s of it into the next shot.")
        elif fill < _FILL_FLOOR and not action.strip():
            verdict = "under"
            notes.append(
                f"shot {position}: only about {round(speech, 1)}s of the {seconds_each}s clip is "
                f"accounted for — the rest is dead air. Add another line, a reaction, a piece of "
                f"action or a camera move.")
        else:
            verdict = "ok"

        row_out = {"shot": position, "seconds": seconds_each, "fills": verdict}
        if speech:
            row_out["speech_seconds"] = round(speech, 1)
            row_out["fill_ratio"] = round(fill, 2)
            row_out["headroom_seconds"] = round(seconds_each - speech, 1)
        out.append(row_out)

    if len(rows) > MAX_CLIPS:
        notes.append(f"only the first {MAX_CLIPS} shots are planned; that is the cap")
    lengths = [r["seconds"] for r in out]
    return {"shots": out, "scene_seconds": lengths, "total_seconds": sum(lengths),
            "clip_count": len(lengths), "seconds_each": seconds_each,
            "target_fill": f"{int(_FILL_FLOOR * 100)}-{int(_FILL_CEILING * 100)}% of each clip",
            "note": " · ".join(notes)}


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


# What must NEVER change, said separately from what the shot is free to change.
# The continuity clause used to order "keep its subject, wardrobe, LOCATION,
# LIGHTING and framing exactly" and then hand Sora a scene that moved to a hill
# at twilight in the rain — a prompt at war with itself. A model resolves that by
# regenerating, and what it regenerates is the cast: a five-shot children's
# animation ended with different animals than it started with. Pin the people,
# let the story move.
_CAST_CONTRACT = (
    "CAST — this never changes, whatever else the shot changes: the characters are "
    "exactly the ones described in STYLE, with the same designs, faces, proportions, "
    "wardrobe, colours and art style in every shot of this video. Do not redesign, "
    "recast, replace or add characters. When the shot moves to another place, another "
    "time of day or another angle, the same characters go with it, unchanged."
)


def build_clip_prompt(scene: str, style: str, *, index: int, total: int,
                      continues_from_frame: bool, continues_from_remix: bool = False,
                      is_cut: bool = False, from_supplied_image: bool = False,
                      remix_source_shot: int = 0) -> str:
    """Assemble one clip's prompt: style + cast contract + continuity + this scene.

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
    if style.strip() and total > 1:
        # On every shot, including the first and including a shot whose chain
        # broke — a clip rendered from the prompt alone is exactly where the cast
        # is most likely to change, so it needs this most.
        parts.append(_CAST_CONTRACT)
    if from_supplied_image:
        parts.append(
            "SOURCE IMAGE: the supplied reference is a still of THIS story's "
            "subject and setting. Match its characters, wardrobe, colours and art "
            "style exactly, and animate the action described below from it. It is "
            "a reference for how things LOOK, not a frame to resume — do not treat "
            "it as a paused video."
        )
    elif is_cut and continues_from_remix:
        # A cut is still EDITED from an earlier clip — that is what keeps the cast
        # and the world identical across the cut. The source supplies the look;
        # the prompt has to be explicit that the MOMENT is new, or the model
        # produces another take of the shot it was handed.
        parts.append(
            f"CUT: the video you are editing is shot {remix_source_shot} of this "
            f"sequence, and it is there ONLY to fix the look — same characters, "
            f"wardrobe, world and art style. This is a NEW shot: a different angle, "
            f"subject or moment. Do NOT continue or repeat that shot's action or "
            f"framing. Show what is described below as its own moment, in whatever "
            f"place, light and time of day it describes."
        )
    elif is_cut:
        # A cut with nothing to edit from: the style block is all that holds it.
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
        which = (f"shot {remix_source_shot} of this sequence" if remix_source_shot
                 else "the PREVIOUS shot of this sequence")
        parts.append(
            f"CONTINUITY: the video you are editing is {which}. Keep its cast, "
            f"wardrobe, world and art style exactly, and ADVANCE the action to what "
            f"is described below — this is the NEXT moment, not another take of the "
            f"same one. Do not restart the scene or repeat the previous action. "
            f"Framing, location, time of day and lighting follow the shot below: "
            f"change them where it asks, leave them as they are where it does not."
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
    orientation: str = "portrait", continuity: str = "remix",
    scene_seconds: list[int] | None = None, reference_asset_path: str = "",
    reference_asset_paths: list[str] | None = None,
    scene_continuity: list[str] | None = None,
    scene_remix_from: list[int] | None = None,
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
    mode = (continuity or "remix").lower()

    # Per-shot continuity. "" inherits the sequence mode; "cut" makes this shot a
    # deliberate new angle. A sequence that continues from every shot has no
    # cuts, which is why a trailer built that way felt like one long take with
    # repeats in it.
    per_shot_mode = list(scene_continuity or [])
    while len(per_shot_mode) < len(scenes):
        per_shot_mode.append("")
    per_shot_mode = [(m or "").strip().lower() for m in per_shot_mode[:len(scenes)]]

    # WHICH earlier shot each shot remixes. 0 (the default) means "the one just
    # before". This is a separate axis from `scene_continuity`: that says what the
    # prompt ASKS FOR (continue or cut), this says where the PIXELS come from.
    # Returning to shot 1 after a divergence is how a sequence comes back to its
    # opening framing instead of drifting further with every link in the chain.
    remix_from = list(scene_remix_from or [])
    while len(remix_from) < len(scenes):
        remix_from.append(0)
    remix_from = [int(v or 0) for v in remix_from[:len(scenes)]]

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
    previous_job_id = ""       # the shot just rendered — the DEFAULT remix source
    # Every shot's job id by 1-based index, so a later shot can remix any earlier
    # one rather than only its neighbour. A shot that failed leaves "" here and is
    # not offered as a source.
    job_by_shot: dict[int, str] = {}
    # Supplied pictures Sora has actually ACCEPTED, by shot. A refusal is about
    # the picture, not the account, so one that already went through is known
    # usable — the only substitute worth spending a create on.
    accepted_seeds: dict[int, bytes] = {}

    reference: bytes | None = None      # the previous shot's final frame
    refused_seeds: list[int] = []
    failed_shots: list[dict] = []
    timing_notes: list[str] = []
    # Sora refuses ANY input_reference showing a human face, whoever drew it. One
    # refusal tells us this sequence has people in it, so the remaining shots go
    # straight to remix instead of paying for the same refusal shot after shot.
    frames_refused = False
    previous_seconds = 0.0     # what the last clip ACTUALLY came out at

    # A remix takes only a prompt and inherits its source's duration, so a remixed
    # sequence is uniform whatever was asked for. Say it ONCE, up front, rather
    # than letting the agent discover it from per-shot requested_seconds — the
    # fix is to write more into each shot, not to ask for a different length.
    if len(set(lengths)) > 1 and (mode == "remix"
                                  or any(m == "remix" for m in per_shot_mode)):
        timing_notes.append(
            f"lengths {lengths} were requested, but a remix inherits its source clip's "
            f"duration, so every chained shot renders at {lengths[0]}s. Pick ONE clip "
            f"length and write each scene to fill it.")

    for index, (scene, seconds) in enumerate(zip(scenes, lengths), start=1):
        shot_mode = per_shot_mode[index - 1] or mode
        is_cut = shot_mode in {"cut", "none"} and index > 1
        seed = seeds[index - 1]

        # Which earlier clip this shot is edited FROM. Named shot, else the one
        # just before. A source that does not exist yet (or failed) falls back to
        # the previous shot rather than refusing the whole shot.
        wanted_source = remix_from[index - 1]
        if wanted_source and 1 <= wanted_source < index and job_by_shot.get(wanted_source):
            source_job, source_shot = job_by_shot[wanted_source], wanted_source
        else:
            if wanted_source and index > 1:
                timing_notes.append(
                    f"shot {index}: asked to remix shot {wanted_source}, which is not "
                    f"available; used the previous shot instead.")
            source_job, source_shot = previous_job_id, index - 1

        # A picture the agent chose for THIS shot wins over the chained frame:
        # naming that panel is a more specific instruction than "continue from
        # the last shot", and the shared style block still carries the look.
        if seed is not None:
            active, from_image = seed, True
        elif is_cut or (frames_refused and mode in {"auto", "frame"}):
            active, from_image = None, False
        else:
            active, from_image = (reference if mode in {"auto", "frame"} else None), False

        use_frame = active is not None
        prompt = build_clip_prompt(scene, style, index=index, total=len(scenes),
                                   continues_from_frame=use_frame and not from_image,
                                   is_cut=is_cut, from_supplied_image=from_image)
        clip = job_id = None
        how = ""

        # Remix is the chain: the model EDITS an earlier clip of this same
        # sequence rather than making a new one, so the cast, wardrobe, lighting
        # and world carry over. It is the only continuity lever that survives
        # Sora's refusal of reference images with faces in them, so it is the
        # default for every shot after the first.
        #
        # A CUT is still remixed — the source picks the look, the prompt asks for
        # a new moment. Those are separate axes, and treating a cut as "no remix"
        # is what made cuts drift into a different film.
        #
        # "auto" joins this path once a face has been refused: from then on remix
        # is all that is left, and going straight to it saves a refused create.
        # A CUT is remixed only when the SEQUENCE is chaining at all: with
        # continuity="none" nothing is chained, cuts included.
        chaining = mode in {"remix", "auto"}
        wants_remix = (shot_mode == "remix"
                       or (shot_mode == "cut" and chaining)
                       or (frames_refused and mode == "auto"
                           and shot_mode not in {"none", "frame"}))
        if (index > 1 and wants_remix and seed is None
                and resource is not None and source_job):
            try:
                remix_prompt = build_clip_prompt(
                    scene, style, index=index, total=len(scenes),
                    continues_from_frame=False, continues_from_remix=True,
                    is_cut=is_cut, remix_source_shot=source_shot)
                clip, job_id = await remix_clip(resource, source_job, remix_prompt)
                how = f"remix(shot {source_shot})"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Remix of %s failed (%s); creating instead", source_job, exc)

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
                # A refused SUPPLIED image falls back to the sequence's own
                # continuity, in that order of preference. The picture is out of
                # play either way — Sora will not look at it — so the only
                # question left is what anchors this shot, and ANY anchor from
                # this sequence beats none. Rendering it from the prompt alone is
                # what dropped two strangers into the middle of a seven-shot
                # trailer: shots 2 and 6 were the only unanchored ones, and they
                # were the only ones whose cast changed.
                if from_image and _looks_like_reference_rejection(detail):
                    logger.info("Reference image refused for shot %d (%s); falling back "
                                "to this sequence's own continuity", index, detail[:160])
                    refused_seeds.append(index)
                    frames_refused = True
                    # 1. Edit an earlier clip of this sequence. The strongest
                    #    anchor there is: it carries the cast, wardrobe, world and
                    #    light the refused panel was picked to supply.
                    if index > 1 and resource is not None and source_job:
                        try:
                            retry_prompt = build_clip_prompt(
                                scene, style, index=index, total=len(scenes),
                                continues_from_frame=False, continues_from_remix=True,
                                is_cut=is_cut, remix_source_shot=source_shot)
                            clip, job_id = await remix_clip(resource, source_job,
                                                            retry_prompt)
                            how = f"remix(shot {source_shot}, image refused)"
                        except Exception as remix_exc:  # noqa: BLE001
                            logger.warning("Remix after a refused image failed too: %s",
                                           remix_exc)
                    # 2. Another picture Sora has already taken from this
                    #    sequence. A different panel of the same story is a poorer
                    #    match than the one chosen for this shot, and still far
                    #    better than an unanchored clip.
                    if clip is None and accepted_seeds and resource is not None:
                        donor = max(accepted_seeds)
                        try:
                            sub_prompt = build_clip_prompt(
                                scene, style, index=index, total=len(scenes),
                                continues_from_frame=False, is_cut=is_cut,
                                from_supplied_image=True)
                            clip, job_id = await create_clip(
                                resource, sub_prompt, seconds, size,
                                accepted_seeds[donor])
                            how = f"create+image(shot {donor}'s, image refused)"
                        except Exception as sub_exc:  # noqa: BLE001
                            logger.warning("Substitute reference from shot %d was "
                                           "refused too: %s", donor, sub_exc)
                    # 3. Nothing to anchor to — the prompt and `style` alone.
                    if clip is None:
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
                elif (use_frame and mode == "auto" and source_job and resource is not None
                        and _looks_like_reference_rejection(detail)):
                    # Every later shot now skips the frame and remixes directly.
                    frames_refused = True
                    logger.info("Reference refused for shot %d (%s); remixing shot %d (%s) "
                                "instead", index, detail[:160], source_shot, source_job)
                    try:
                        retry_prompt = build_clip_prompt(
                            scene, style, index=index, total=len(scenes),
                            continues_from_frame=False, continues_from_remix=True,
                            is_cut=is_cut, remix_source_shot=source_shot)
                        clip, job_id = await remix_clip(resource, source_job, retry_prompt)
                        how = f"remix(shot {source_shot})"
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
        if job_id:
            job_by_shot[index] = job_id
        if seed is not None and how == "create+image":
            accepted_seeds[index] = seed

        # Measure what was ACTUALLY rendered. A remix takes only a prompt and
        # inherits its source's duration, so a shot that asked for 8s and fell
        # back to remixing a 4s clip is 4s — reporting the request would be a lie,
        # and the agent writes its caption from these numbers.
        try:
            actual = round(await asyncio.to_thread(video.duration_seconds, clip), 1)
        except Exception as exc:  # noqa: BLE001 - never fail a run over measurement
            logger.warning("Could not measure shot %d: %s", index, exc)
            actual = float(seconds)
        previous_seconds = actual
        detail_row = {"shot": index, "seconds": actual, "how": how, "job_id": job_id}
        if abs(actual - seconds) >= 1:
            detail_row["requested_seconds"] = seconds
        details.append(detail_row)
        logger.info("Shot %d/%d done (%s, %ss%s, %d bytes)", index, len(scenes), how, actual,
                    f" — {seconds}s requested" if "requested_seconds" in detail_row else "",
                    len(clip))

        # Keep the tail frame for whichever later shot wants to continue from it.
        # Pointless once a face has been refused — every later frame from this
        # sequence would be refused for the same reason, and extraction is not free.
        if frames_refused and mode in {"auto", "frame"}:
            reference = None
        elif mode in {"auto", "frame"} or any(m == "" for m in per_shot_mode[index:]):
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
        # Exact match: a shot that fell back to ANOTHER shot's picture did not get
        # the one it was given, and counting it here would report a match that was
        # never made.
        used = sum(1 for row in details if row.get("how", "") == "create+image")
        result["reference_images_used"] = used
        result["reference_images_given"] = asked_for
        notes = list(seed_notes)
        if refused_seeds:
            notes.append(
                f"Sora refused the reference image on shot(s) "
                f"{', '.join(str(i) for i in refused_seeds)} — it rejects images "
                f"containing human faces. Those shots fell back to this sequence's own "
                f"continuity instead (`how` says what each one used); describe the "
                f"character IN `style` too, since that is what survives every refusal.")
        stranded = [row["shot"] for row in details
                    if row.get("how", "") == "create(image refused)"]
        if stranded:
            notes.append(
                f"shot(s) {', '.join(str(s) for s in stranded)} had no earlier clip and "
                f"no accepted picture to fall back on, so they were rendered from the "
                f"prompt and `style` alone — that is where the look is most likely to "
                f"have changed. Check them before publishing.")
        if notes:
            result["reference_notes"] = notes

    # A supplied picture WINS over the chain for its shot, so a sequence carrying
    # one on every shot is never chained at all and `scene_remix_from` — which the
    # agent sat and planned — is silently ignored. On the trailer that prompted
    # this, all seven shots had a panel and not one shot was remixed.
    unchained = [row["shot"] for row in details
                 if row.get("how", "") == "create+image" and row["shot"] > 1]
    if unchained and mode in {"remix", "auto"}:
        timing_notes.append(
            f"shot(s) {', '.join(str(s) for s in unchained)} used their own reference "
            f"image, so they were NOT chained from an earlier shot — a supplied image "
            f"wins over the remix, and scene_remix_from does not apply to them. Their "
            f"consistency rests on those pictures and on `style`.")

    # Every forward link is another generation away from the opening, so a long
    # unbroken chain drifts — which is how a five-shot children's animation ended
    # with a different cast than it began with, every shot correctly remixed.
    longest = run = 0
    for row in details:
        how = row.get("how", "")
        if how.startswith(f"remix(shot {row['shot'] - 1}"):
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    if longest >= _CHAIN_DRIFT_LINKS:
        timing_notes.append(
            f"{longest} shots in a row each remixed the shot just before them, so the "
            f"last of them is {longest} generations from where the video started and "
            f"the look drifts a little at every link. If the characters or the style "
            f"changed by the end, re-run with scene_remix_from anchoring the later "
            f"shots to an early one that shows them clearly — e.g. [0, 0, 1, 1, 1] "
            f"rather than all zeros.")

    warnings = []
    if timing_notes:
        result["timing_notes"] = timing_notes
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

        This is arithmetic about a TARGET LENGTH. If the video has dialogue, the
        words decide the shape instead: use ``plan_shot_timing``, and treat the
        total it reports as the video's real length.

        Returns the segment lengths, the clip count, and the total actually
        achievable — check ``note``, since not every duration is reachable exactly.
        The plan is a STARTING POINT: vary the real lengths per shot with
        ``scene_seconds`` so the pacing matches the story rather than a grid.
        """
        return plan_segments(target_seconds, prefer_clip_seconds)

    return plan_video


def _make_plan_shot_lengths(state: dict):
    @function_tool
    async def plan_shot_timing(dialogue: list[str], action: list[str] | None = None,
                               seconds_each: int = 12) -> dict:
        """Check every shot has enough scenario to FILL its clip — and not too much.

        Clips are a fixed length, so the WRITING has to match the clip, not the
        other way round. Call this on your shot list before
        ``create_video_sequence``, and rewrite the shots it flags.

        Args:
            dialogue: One entry per shot, in scene order — the ACTUAL words spoken
                in that shot ("" if nobody speaks). Write the real line; a summary
                of it times differently from the line itself.
            action: Optional, one per shot. What happens on screen. A shot with no
                dialogue is not flagged as empty if something is happening in it.
            seconds_each: The clip length for the whole video (4, 8 or 12). One
                length for every shot — a remixed chain inherits its source's
                duration, so it could not vary even if you asked.

        Each shot comes back with ``fills``:

        * ``"under"`` — dead air. Give that shot more: another line, a reaction,
          an action beat, a camera move.
        * ``"ok"`` — inside the margin.
        * ``"over"`` — the line will be cut off mid-sentence. Move the surplus
          into the next shot; ``note`` says roughly how many seconds of it.

        Aim for the reported ``target_fill``, never 100%: the margin is what
        protects you when the delivery runs slower than the arithmetic.
        """
        lines = list(dialogue or [])
        actions = list(action or [])
        rows = [{"dialogue": lines[i] if i < len(lines) else "",
                 "action": actions[i] if i < len(actions) else ""}
                for i in range(max(len(lines), len(actions)))]
        return plan_shot_lengths(rows, seconds_each)

    return plan_shot_timing


def _make_create_sequence(state: dict):
    if not sora_config.enabled():
        return None

    @function_tool
    async def create_video_sequence(
        scenes: list[str],
        style: str = "",
        seconds_each: int = 12,
        orientation: str = "portrait",
        continuity: str = "remix",
        scene_seconds: list[int] | None = None,
        reference_asset_path: str = "",
        reference_asset_paths: list[str] | None = None,
        scene_continuity: list[str] | None = None,
        scene_remix_from: list[int] | None = None,
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
            seconds_each: The clip length for the WHOLE video (snapped to 4, 8 or
                12); a remixed chain renders every shot at this length. Prefer 12:
                fewer, longer shots read as film, many 4s shots read as a
                slideshow, and each cut is a seam. The video is therefore
                ``len(scenes) × seconds_each`` — decide that total first, then
                write the story to fill it.
            orientation: "portrait" for Reels/TikTok/Shorts, "landscape" otherwise.
            continuity: The DEFAULT for shots that don't say otherwise. **"remix"
                (the default) is what you want**: every shot after the first is
                the model EDITING an earlier clip of this same sequence, so the
                cast, wardrobe, world and lighting carry over — the only lever
                that survives Sora refusing reference images with faces in them.
                Cuts are remixed too: the source fixes the look, the prompt asks
                for a new moment. "auto" tries frame chaining first and switches
                to remix the moment a reference is refused. "frame" chains from
                final frames only (face-free material). "none" makes every shot
                independent — use it only when the shots share nothing.

                A remix cannot change the clip length; it inherits its source's.
                That is why the video is n × ``seconds_each``, and why the fix for
                bad pacing is writing, not lengths.
            scene_remix_from: Which earlier shot each shot is edited FROM, 1-based;
                ``0`` (the default) means the shot just before it. This is a
                separate decision from ``scene_continuity``: that says what the
                prompt ASKS FOR, this says where the pixels come from. Chaining
                from the previous shot advances the action but drifts a little
                further with every link — so when a shot returns to the opening
                framing, the establishing wide, or a character last seen at the
                start, remix it from THAT shot instead: ``[0, 0, 1, 0, 1]``. A
                source that does not exist or whose shot failed falls back to the
                previous shot and says so in ``timing_notes``.
            scene_seconds: Per-shot lengths. Only meaningful for shots that are
                NOT remixed — a remix inherits its source clip's duration, so a
                chained sequence renders every shot at ``seconds_each`` whatever
                you put here. Pick one clip length for the video and write each
                scene to fill it; that is what ``plan_shot_timing`` is for.
            scene_continuity: Per-shot direction, one entry per scene. "" (the
                default) uses ``continuity``. **"cut"** makes that shot a
                deliberate new angle or moment — same world, same characters, same
                style, but not a continuation. Use "cut" whenever the story moves
                to a different place, subject or time; forcing continuity across a
                jump is what produces gaps and repeated action. "remix" derives
                this shot from the previous one.
            reference_asset_paths: One image per shot, same order as ``scenes``;
                use "" for shots with no image.

                **Only for material with NO people in it.** Sora rejects any
                reference image containing a human face — including one you just
                drew with ``generate_image``, because who made it is irrelevant.
                Do not build a character sheet to pass in here and do not paint
                opening frames of people; it is refused. Useful for locations,
                objects, artwork, logos and landscapes. When people are on camera,
                identity comes from ``style`` and continuity comes from remix.

                A refused image does not leave the shot adrift: it falls back to
                remixing an earlier shot, then to a picture already accepted in
                this sequence, then to the prompt alone — ``how`` says which, and
                only the last of those is a consistency risk. But note that a shot
                WITH an accepted picture is not chained at all, so giving every
                shot its own image opts the whole video out of remix.
            reference_asset_path: Shorthand for a single image on shot 1.

        Returns the merged ``asset_path``, its **measured** duration, and per-shot
        detail: ``how`` says whether a shot used its image ("create+image"), the
        previous frame ("create+frame"), a remix, or nothing.

        Read the result before captioning. ``how`` says how each shot was made,
        including which shot it was remixed from. ``reference_notes`` names shots
        whose reference image was refused for containing a face — expected, not a
        failure. ``timing_notes`` covers anything that did not happen as directed.
        Caption the REAL ``duration_seconds``, never the planned one. Sora 2 has
        no seed, so shots are plausibly the same scene rather than identical.
        """
        if state.get("video_failures", 0) >= 2:
            return {"error": "video_circuit_open",
                    "message": "Video generation failed repeatedly this run."}
        result = await perform_create_sequence(
            state, scenes, style=style, seconds_each=seconds_each,
            orientation=orientation, continuity=continuity, scene_seconds=scene_seconds,
            reference_asset_path=reference_asset_path,
            reference_asset_paths=reference_asset_paths,
            scene_continuity=scene_continuity, scene_remix_from=scene_remix_from)
        if result.get("error"):
            state["video_failures"] = state.get("video_failures", 0) + 1
        return result

    return create_video_sequence


register_tool("plan_video", _make_plan_video)
register_tool("plan_shot_timing", _make_plan_shot_lengths)
register_tool("create_video_sequence", _make_create_sequence)
