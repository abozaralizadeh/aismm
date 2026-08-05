"""Long videos from short Sora clips: planning, consistency, merging.

The Sora client is mocked, so these pin the *consistency machinery* — which is
where GenBox drifts: the style block must reach every prompt, the reference frame
must be described as a continuation, the sequence must stay on one resource so
remix remains possible, and a refused reference must fall back to remixing rather
than making an unrelated clip.

They also pin the two faults that shipped a bad reel to a live account: every
fallback remixed **shot 1**, so the opening moment played three times over; and a
remix silently inherits its source's duration, so a 4/4/4/8 plan rendered 16s
while reporting the 20s that was asked for.
"""
import asyncio

import pytest

from aismm.tools import sequence_tool
from aismm.tools.sequence_tool import (
    ALLOWED_SECONDS, MAX_CLIPS, build_clip_prompt, plan_segments,
)


# --- planning: the agent decides the length ------------------------------------------ #

@pytest.mark.parametrize("target,expected_total", [
    (12, 12), (24, 24), (60, 60), (48, 48), (8, 8),
])
def test_reachable_durations_are_hit_exactly(target, expected_total):
    plan = plan_segments(target)
    assert plan["total_seconds"] == expected_total
    assert all(s in ALLOWED_SECONDS for s in plan["segments"])


def test_a_minute_is_five_twelve_second_clips():
    """The example from the brief: "one minute" is a merge, not one clip."""
    plan = plan_segments(60)
    assert plan["segments"] == [12, 12, 12, 12, 12]
    assert plan["clip_count"] == 5


def test_unreachable_duration_says_so_rather_than_silently_differing():
    plan = plan_segments(30)
    assert plan["total_seconds"] == 32
    assert "not reachable" in plan["note"] and "32" in plan["note"]


def test_shorter_than_the_minimum_clip():
    plan = plan_segments(3)
    assert plan["segments"] == [min(ALLOWED_SECONDS)]
    assert "minimum" in plan["note"]


def test_absurd_length_is_capped_with_an_explanation():
    plan = plan_segments(600)
    assert plan["clip_count"] == MAX_CLIPS
    assert "capped" in plan["note"]


def test_preferring_shorter_clips_gives_finer_pacing():
    plan = plan_segments(24, prefer=4)
    assert plan["segments"] == [4] * 6


def test_zero_or_negative_is_rejected():
    for target in (0, -5):
        assert plan_segments(target)["segments"] == []


# --- prompt assembly: the consistency levers ------------------------------------------ #

STYLE = "A calm 40-year-old presenter in a navy suit, glossy dark desk, soft key light"


def test_style_is_repeated_in_every_shot():
    """Lever 1 — GenBox put the style only in the first clip of a speaker."""
    for index in (1, 2, 5):
        prompt = build_clip_prompt("she turns to camera", STYLE, index=index, total=5,
                                   continues_from_frame=False)
        assert STYLE in prompt


def test_a_reference_frame_is_described_as_a_continuation():
    """Lever 2 — passing the image without saying what it is lets Sora drift."""
    prompt = build_clip_prompt("she picks up the kite", STYLE, index=2, total=3,
                               continues_from_frame=True)
    assert "FINAL FRAME of the previous shot" in prompt
    assert "do not cut to a new location" in prompt.lower() or \
           "not cut to a new location" in prompt


def test_later_shots_without_a_frame_still_get_continuity_language():
    prompt = build_clip_prompt("she smiles", STYLE, index=3, total=4,
                               continues_from_frame=False)
    assert "SAME scene and subject" in prompt


def test_the_first_shot_has_no_continuity_clause():
    prompt = build_clip_prompt("she enters", STYLE, index=1, total=3,
                               continues_from_frame=False)
    assert "CONTINUITY" not in prompt


def test_shot_position_is_stated():
    assert "SHOT 2 of 4" in build_clip_prompt("x", STYLE, index=2, total=4,
                                              continues_from_frame=False)


def test_prompts_forbid_baked_in_text():
    prompt = build_clip_prompt("x", STYLE, index=1, total=1, continues_from_frame=False)
    assert "no on-screen text" in prompt.lower()


# --- sequence generation -------------------------------------------------------------- #

RESOURCE_A = {"endpoint": "https://a.openai.azure.com", "key": "k", "model": "sora-2"}
RESOURCE_B = {"endpoint": "https://b.openai.azure.com", "key": "k", "model": "sora-2"}


@pytest.fixture()
def sora(monkeypatch, tmp_path):
    """Record every Sora call; return fake clips without touching ffmpeg or the API."""
    calls = {"creates": [], "remixes": [], "failover": 0}

    async def failover(prompt, seconds, size, *, ref_image_bytes=None, max_attempts=None):
        calls["failover"] += 1
        calls["creates"].append({"prompt": prompt, "seconds": seconds, "size": size,
                                 "reference": ref_image_bytes, "resource": RESOURCE_A})
        return b"clip", f"job-{len(calls['creates'])}", RESOURCE_A

    async def create(resource, prompt, seconds, size, reference=None):
        calls["creates"].append({"prompt": prompt, "seconds": seconds, "size": size,
                                 "reference": reference, "resource": resource})
        return b"clip", f"job-{len(calls['creates'])}"

    async def remix(resource, base_job_id, prompt):
        calls["remixes"].append({"base": base_job_id, "prompt": prompt,
                                 "resource": resource})
        # A remix inherits its SOURCE's duration; the caller's request is ignored.
        return b"remixed", f"remix-{len(calls['remixes'])}"

    monkeypatch.setattr(sequence_tool, "create_clip_with_failover", failover)
    monkeypatch.setattr(sequence_tool, "create_clip", create)
    monkeypatch.setattr(sequence_tool, "remix_clip", remix)
    monkeypatch.setattr(sequence_tool.video, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(sequence_tool.video, "extract_last_frame",
                        lambda clip, size: b"frame-bytes")
    monkeypatch.setattr(sequence_tool.video, "concat_clips",
                        lambda clips, size: b"merged" * len(clips))
    # Per-clip vs merged: a created clip renders the 8s asked for, a remix comes
    # back at its source's 4s — the real behaviour that shipped a 16s "20s" video.
    monkeypatch.setattr(sequence_tool.video, "duration_seconds",
                        lambda data: 24.0 if data.startswith(b"merged")
                        else (4.0 if data == b"remixed" else 8.0))
    monkeypatch.setattr(sequence_tool, "save_bytes",
                        lambda data, ext: str(tmp_path / f"merged.{ext}"))
    monkeypatch.setattr(sequence_tool, "public_url", lambda p: f"https://host/{p}")
    return calls


def _sequence(state=None, **kwargs):
    kwargs.setdefault("style", STYLE)
    return asyncio.run(sequence_tool.perform_create_sequence(
        state if state is not None else {},
        kwargs.pop("scenes", ["shot one", "shot two", "shot three"]), **kwargs))


def test_every_scene_becomes_a_clip_and_they_are_merged(sora):
    result = _sequence()
    assert result["clips_merged"] == 3
    assert result["asset_path"].endswith(".mp4")
    assert result["duration_seconds"] == 24.0


def test_the_style_reaches_every_generated_prompt(sora):
    _sequence()
    assert len(sora["creates"]) == 3
    assert all(STYLE in call["prompt"] for call in sora["creates"])


def test_the_last_frame_is_chained_into_later_shots(sora):
    """Shot 1 has no reference; shots 2+ start from the previous final frame."""
    _sequence(continuity="auto")
    assert sora["creates"][0]["reference"] is None
    assert sora["creates"][1]["reference"] == b"frame-bytes"
    assert sora["creates"][2]["reference"] == b"frame-bytes"


def test_the_sequence_is_pinned_to_one_resource(sora):
    """GenBox rotated resources per clip, which makes remix impossible."""
    _sequence()
    assert sora["failover"] == 1                     # only shot 1 picks a resource
    assert all(call["resource"] is RESOURCE_A for call in sora["creates"][1:])


def test_remix_mode_chains_each_shot_from_the_previous_one(sora):
    """NOT from shot 1.

    Anchoring every remix to shot 1 shipped a reel whose opening moment played
    three times: each later shot applied its own prompt to the same untouched
    starting point, so the action never advanced.
    """
    _sequence(continuity="remix")
    assert len(sora["creates"]) == 1                 # only the base clip is created
    assert len(sora["remixes"]) == 2
    assert [r["base"] for r in sora["remixes"]] == ["job-1", "remix-1"]
    assert all(STYLE in r["prompt"] for r in sora["remixes"])


def test_a_remixed_shot_is_told_it_is_continuing_not_restating(sora):
    _sequence(continuity="remix")
    prompt = sora["remixes"][1]["prompt"]
    assert "PREVIOUS shot" in prompt
    assert "NEXT moment, not another take" in prompt


def test_no_continuity_mode_passes_no_reference(sora):
    _sequence(continuity="none")
    assert all(call["reference"] is None for call in sora["creates"])


@pytest.fixture()
def faces_refused(monkeypatch, sora):
    """Azure rejects input_reference containing faces — the real clinic-video case.

    Every shot after the first refuses, so `auto` degrades to remix throughout;
    that is exactly what happened on the account. Depends on ``sora`` so it wraps
    the mocked ``create_clip`` rather than being overwritten by it.
    """
    original = sequence_tool.create_clip

    async def refuse_reference(resource, prompt, seconds, size, reference=None):
        if reference is not None:
            raise RuntimeError("input_reference rejected: human face detected")
        return await original(resource, prompt, seconds, size, reference)

    monkeypatch.setattr(sequence_tool, "create_clip", refuse_reference)


def test_a_refused_reference_falls_back_to_remixing(faces_refused, sora):
    result = _sequence(scenes=["one", "two"], continuity="auto")
    assert result["clips_merged"] == 2               # nothing was lost
    assert sora["remixes"] and sora["remixes"][0]["base"] == "job-1"
    assert result["shots"][1]["how"] == "remix(fallback)"


def test_the_fallback_chain_never_replays_shot_one(faces_refused, sora):
    """The shipped bug: four shots, three of them remixes of shot 1.

    The reel opened with the same moment three times over. Each fallback must
    build on the shot before it, so the action moves.
    """
    _sequence(scenes=["one", "two", "three", "four"], continuity="auto")
    assert [r["base"] for r in sora["remixes"]] == ["job-1", "remix-1", "remix-2"]


def test_a_remix_reports_its_real_duration_not_the_requested_one(faces_refused, sora):
    """A remix inherits its source's length, so the request cannot be honoured."""
    result = _sequence(scenes=["one", "two"], continuity="auto", seconds_each=8)
    assert result["shots"][0]["seconds"] == 8.0      # created: got what it asked for
    assert result["shots"][1]["seconds"] == 4.0      # remixed: inherited 4s
    assert result["shots"][1]["requested_seconds"] == 8


def test_a_shortfall_is_warned_about_rather_than_left_to_be_miscaptioned(faces_refused, sora):
    result = _sequence(scenes=["one", "two"], continuity="auto", seconds_each=8)
    assert "did not render at the requested length" in result["warning"]
    assert "REAL duration" in result["warning"]


def test_no_shortfall_warning_when_every_shot_got_its_length(sora):
    result = _sequence(continuity="none", seconds_each=8)
    assert "warning" not in result
    assert all("requested_seconds" not in shot for shot in result["shots"])


def test_a_failing_shot_keeps_the_clips_already_made(monkeypatch, sora):
    async def fail_after_first(resource, prompt, seconds, size, reference=None):
        raise RuntimeError("Sora job failed: content filtered")

    monkeypatch.setattr(sequence_tool, "create_clip", fail_after_first)
    result = _sequence(scenes=["one", "two", "three"], continuity="none")

    assert result["clips_merged"] == 1
    assert "only 1 of 3" in result["warning"]


def test_a_failing_first_shot_is_an_error_not_an_empty_video(monkeypatch, sora):
    async def fail(prompt, seconds, size, *, ref_image_bytes=None, max_attempts=None):
        raise RuntimeError("all resources exhausted")

    monkeypatch.setattr(sequence_tool, "create_clip_with_failover", fail)
    result = _sequence()
    assert result["error"] == "video_generation_failed"


def test_per_shot_lengths_are_honoured(sora):
    _sequence(scenes=["a", "b"], scene_seconds=[4, 12])
    assert [call["seconds"] for call in sora["creates"]] == [4, 12]


def test_seconds_are_snapped_to_what_sora_accepts(sora):
    _sequence(scenes=["a"], seconds_each=7)
    assert sora["creates"][0]["seconds"] in ALLOWED_SECONDS


def test_orientation_picks_the_size(sora):
    _sequence(scenes=["a"], orientation="landscape")
    width, height = (int(v) for v in sora["creates"][0]["size"].split("x"))
    assert width > height


def test_too_many_scenes_are_capped(sora):
    _sequence(scenes=[f"shot {i}" for i in range(20)], continuity="none")
    assert len(sora["creates"]) == MAX_CLIPS


def test_empty_scenes_are_rejected(sora):
    assert _sequence(scenes=["", "  "])["error"] == "no_scenes"


def test_shot_detail_reports_how_each_was_made(sora):
    result = _sequence()
    assert [s["shot"] for s in result["shots"]] == [1, 2, 3]
    assert all(s["how"] for s in result["shots"])


def test_missing_ffmpeg_is_reported_clearly(monkeypatch, sora):
    monkeypatch.setattr(sequence_tool.video, "ffmpeg_available", lambda: False)
    result = _sequence()
    assert result["error"] == "ffmpeg_missing"
    assert "imageio-ffmpeg" in result["message"]


def test_the_merged_asset_is_recorded_on_state(sora):
    state = {}
    _sequence(state)
    assert state["assets"][0]["kind"] == "video"
    assert state["assets"][0]["shots"]


# --- the prompt tells the agent not to waste a generation ----------------------------- #

def test_the_prompt_forbids_a_throwaway_clip_before_a_sequence():
    """A run generated a 12s clip, ignored it, built a sequence, and posted that."""
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS

    assert "DECIDE THE SHAPE BEFORE" in MANAGER_INSTRUCTIONS
    assert "to see how it looks" in MANAGER_INSTRUCTIONS


def test_the_prompt_warns_that_shots_must_advance():
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS

    assert "NEXT step in the action" in MANAGER_INSTRUCTIONS
    assert "repeats itself" in MANAGER_INSTRUCTIONS


def test_the_prompt_points_at_the_duration_warning():
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS

    assert "check `warning`" in MANAGER_INSTRUCTIONS


# --- one bad shot must not discard the rest ------------------------------------------ #
# Reported: a nine-shot trailer came back as a 12-second stub — "only 1 of 9 shots
# rendered". Shot 2 failed and the loop abandoned shots 3-9 without attempting
# them. A sequence is independent clips; one failure is a gap, not the end.

def _flaky(monkeypatch, tmp_path, failing: set[int]):
    """Sora that fails on the given 1-based shot numbers."""
    calls = {"attempts": 0}

    async def failover(prompt, seconds, size, *, ref_image_bytes=None, **kw):
        calls["attempts"] += 1
        if calls["attempts"] in failing:
            raise RuntimeError(f"shot {calls['attempts']} exploded")
        return b"clip", f"job-{calls['attempts']}", RESOURCE_A

    async def create(resource, prompt, seconds, size, reference=None):
        calls["attempts"] += 1
        if calls["attempts"] in failing:
            raise RuntimeError(f"shot {calls['attempts']} exploded")
        return b"clip", f"job-{calls['attempts']}"

    monkeypatch.setattr(sequence_tool, "create_clip_with_failover", failover)
    monkeypatch.setattr(sequence_tool, "create_clip", create)
    monkeypatch.setattr(sequence_tool.video, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(sequence_tool.video, "extract_last_frame", lambda c, s: b"frame")
    monkeypatch.setattr(sequence_tool.video, "concat_clips",
                        lambda clips, size: b"merged" * len(clips))
    monkeypatch.setattr(sequence_tool.video, "duration_seconds", lambda data: 8.0)
    monkeypatch.setattr(sequence_tool, "save_bytes", lambda data, ext: str(tmp_path / "m.mp4"))
    monkeypatch.setattr(sequence_tool, "public_url", lambda p: "https://host/m.mp4")
    return calls


def test_a_failed_shot_is_skipped_and_the_rest_still_render(monkeypatch, tmp_path):
    calls = _flaky(monkeypatch, tmp_path, failing={2})
    result = asyncio.run(sequence_tool.perform_create_sequence(
        {}, [f"shot {i}" for i in range(1, 6)]))
    assert calls["attempts"] == 5                 # every shot was ATTEMPTED
    assert result["clips_merged"] == 4            # four of five made it
    assert [row["shot"] for row in result["failed_shots"]] == [2]


def test_the_warning_names_which_shots_failed(monkeypatch, tmp_path):
    _flaky(monkeypatch, tmp_path, failing={2})
    result = asyncio.run(sequence_tool.perform_create_sequence(
        {}, [f"shot {i}" for i in range(1, 6)]))
    assert "shot(s) 2 failed" in result["warning"]
    assert "publish them if the result still tells the story" in result["warning"]


def test_a_failure_on_shot_one_no_longer_ends_the_sequence(monkeypatch, tmp_path):
    """It used to return immediately, losing eight shots that were never tried."""
    calls = _flaky(monkeypatch, tmp_path, failing={1})
    result = asyncio.run(sequence_tool.perform_create_sequence(
        {}, [f"shot {i}" for i in range(1, 5)]))
    assert calls["attempts"] == 4
    assert result["clips_merged"] == 3


def test_systemic_failure_stops_rather_than_grinding_through(monkeypatch, tmp_path):
    """A dead resource or an empty account should not burn twelve attempts."""
    calls = _flaky(monkeypatch, tmp_path, failing=set(range(1, 13)))
    result = asyncio.run(sequence_tool.perform_create_sequence(
        {}, [f"shot {i}" for i in range(1, 10)]))
    assert calls["attempts"] == sequence_tool._MAX_SHOT_FAILURES
    assert result["error"] == "video_generation_failed"
    assert "Every shot failed" in result["message"]
