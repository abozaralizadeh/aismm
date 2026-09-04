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
    """Including remix prompts — it is the one lever that survives everything."""
    _sequence()
    prompts = ([c["prompt"] for c in sora["creates"]]
               + [r["prompt"] for r in sora["remixes"]])
    assert len(prompts) == 3
    assert all(STYLE in prompt for prompt in prompts)


def test_the_last_frame_is_chained_into_later_shots(sora):
    """Shot 1 has no reference; shots 2+ start from the previous final frame."""
    _sequence(continuity="auto")
    assert sora["creates"][0]["reference"] is None
    assert sora["creates"][1]["reference"] == b"frame-bytes"
    assert sora["creates"][2]["reference"] == b"frame-bytes"


def test_the_sequence_is_pinned_to_one_resource(sora):
    """GenBox rotated resources per clip, which makes remix impossible."""
    _sequence(continuity="none")
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
    assert "shot 2 of this sequence" in prompt        # which clip it is editing
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
    assert result["shots"][1]["how"] == "remix(shot 1)"


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
    _sequence(scenes=["a", "b"], scene_seconds=[4, 12], continuity="none")
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

    assert "decide the whole thing BEFORE you generate anything" in MANAGER_INSTRUCTIONS
    assert "to see how it looks" in MANAGER_INSTRUCTIONS


def test_the_prompt_warns_that_shots_must_advance():
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS

    assert "the NEXT moment, never a\n      restatement of the last" in MANAGER_INSTRUCTIONS


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


# --- timing the shots from the words they carry --------------------------------------- #
# Reported: "the videos are cut in the middle of speaking and the timings are not
# good". Both come from picking a clip length first and writing the shot into it.
# plan_shot_timing does it the other way round, and always rounds UP: an over-long
# clip is a beat of silence, an under-long one is a cut-off word.

from aismm.tools.sequence_tool import (          # noqa: E402
    estimate_speech_seconds, plan_shot_lengths,
)


def test_a_longer_line_needs_a_longer_clip():
    short = estimate_speech_seconds("I know.")
    long = estimate_speech_seconds(" ".join(["word"] * 40))
    assert short < long


def test_silence_takes_no_time():
    assert estimate_speech_seconds("") == 0
    assert estimate_speech_seconds("   ") == 0


def test_a_shot_with_too_much_dialogue_is_flagged_as_over():
    """The clip length is fixed, so the LINE is what has to move."""
    plan = plan_shot_lengths([{"dialogue": " ".join(["word"] * 40)}], 12)
    assert plan["shots"][0]["fills"] == "over"
    assert "cut off mid-sentence" in plan["note"]
    assert "into the next shot" in plan["note"]


def test_a_nearly_empty_shot_is_flagged_as_dead_air():
    """The other half of the same failure: a clip with nothing in the back end."""
    plan = plan_shot_lengths([{"dialogue": "Hi."}], 12)
    assert plan["shots"][0]["fills"] == "under"
    assert "dead air" in plan["note"]


def test_a_well_filled_shot_is_left_alone():
    plan = plan_shot_lengths([{"dialogue": " ".join(["word"] * 18)}], 12)
    assert plan["shots"][0]["fills"] == "ok"
    assert plan["note"] == ""


def test_the_margin_is_a_band_not_the_clip_length():
    """Writing to exactly 12s is what breaks a sentence when the delivery runs
    slower than the arithmetic — the headroom IS the feature."""
    exactly_full = plan_shot_lengths([{"dialogue": " ".join(["word"] * 25)}], 12)
    assert exactly_full["shots"][0]["speech_seconds"] > 12 * 0.85
    assert exactly_full["shots"][0]["fills"] == "over"


def test_action_saves_a_silent_shot_from_being_called_empty():
    """A shot can be full without anybody speaking."""
    plan = plan_shot_lengths([{"action": "she crosses the room and opens the door"}], 12)
    assert plan["shots"][0]["fills"] == "ok"


def test_the_clip_length_applies_to_every_shot():
    """A remixed chain cannot vary its length, so this does not choose lengths."""
    plan = plan_shot_lengths([{"dialogue": "a"}, {"dialogue": "b"}], 8)
    assert plan["scene_seconds"] == [8, 8]
    assert plan["total_seconds"] == 16
    assert plan["seconds_each"] == 8


def test_the_target_fill_is_reported_so_the_agent_can_aim_at_it():
    plan = plan_shot_lengths([{"dialogue": "a"}], 12)
    assert "%" in plan["target_fill"]


def test_more_shots_than_sora_allows_are_capped_with_a_note():
    plan = plan_shot_lengths([{"action": "x"}] * 20, 12)
    assert plan["clip_count"] == 12 and "cap" in plan["note"]


def test_an_empty_plan_is_not_an_exception():
    assert plan_shot_lengths([], 12)["clip_count"] == 0
    assert plan_shot_lengths(None, 12)["clip_count"] == 0


def test_the_timing_tool_is_registered_and_offered():
    from aismm.tools.registry import registered_tool_names

    assert "plan_shot_timing" in registered_tool_names()


def test_the_prompt_tells_the_agent_to_fill_every_clip():
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS as p

    assert "FILL EVERY CLIP" in p
    assert "plan_shot_timing" in p


# --- remix is the chain, and the length follows it ------------------------------------ #
# A remix takes only a prompt and inherits its source's duration, so a chained
# sequence is uniform. That is accepted rather than worked around: rendering a
# shot fresh to hit a length throws away the only continuity lever there is. The
# video is n x seconds_each, and pacing is fixed in the WRITING.

def test_remix_wins_over_a_requested_length(sora):
    _sequence(scenes=["a", "b"], continuity="remix", scene_seconds=[12, 4])
    assert len(sora["remixes"]) == 1                  # not re-created to hit 4s


def test_varying_lengths_are_called_out_once_up_front(sora):
    """Not discovered per shot after the money is spent."""
    result = _sequence(scenes=["a", "b"], continuity="remix", scene_seconds=[12, 4])
    note = " ".join(result["timing_notes"])
    assert "inherits its source clip's duration" in note
    assert "write each scene to fill it" in note.lower()


def test_a_uniform_plan_says_nothing(sora):
    """The note is a correction, so it must not fire on a correct plan."""
    result = _sequence(scenes=["a", "b"], continuity="remix", scene_seconds=[12, 12])
    assert "timing_notes" not in result


# --- one face refusal switches the rest of the sequence to remix ----------------------- #
# Sora refuses ANY input_reference showing a human face. The refusal is proof this
# material has people in it, so paying for the same rejection on every remaining
# shot is waste — and remix is the only continuity lever left.

@pytest.fixture()
def refuse_frames(monkeypatch, sora):
    """Reject a create that carries a reference image, as Azure does for faces.

    Wraps whatever `sora` installed, and records the refused attempts — the point
    of the switch to remix is that there is exactly ONE of them per sequence.
    """
    import httpx

    real_create = sequence_tool.create_clip
    sora["refused"] = []

    async def create(resource, prompt, seconds, size, reference=None):
        if reference is not None:
            sora["refused"].append({"prompt": prompt, "seconds": seconds})
            request = httpx.Request("POST", "https://a.openai.azure.com/openai/v1/videos")
            response = httpx.Response(400, request=request,
                                      text="input_reference may not contain a human face")
            raise httpx.HTTPStatusError("400", request=request, response=response)
        return await real_create(resource, prompt, seconds, size, reference)

    monkeypatch.setattr(sequence_tool, "create_clip", create)
    return create


def test_a_refused_frame_switches_the_rest_of_the_sequence_to_remix(sora, refuse_frames):
    _sequence(scenes=["a", "b", "c", "d"], continuity="auto")
    # Shot 2 pays for the discovery; shots 3 and 4 go straight to remix without
    # re-offering a frame that is going to be refused for the same reason.
    assert len(sora["refused"]) == 1
    assert len(sora["remixes"]) == 3


def test_the_refusal_is_reported_as_expected_not_as_a_failure(sora, refuse_frames):
    result = _sequence(scenes=["a", "b", "c"], continuity="auto")
    assert result["clips_merged"] == 3
    assert not result.get("failed_shots")


def test_a_refused_frame_remixes_the_chosen_source(sora, refuse_frames):
    """The fallback is a remix of the shot the agent nominated, not always the
    neighbour."""
    _sequence(scenes=["a", "b", "c"], continuity="auto", scene_remix_from=[0, 0, 1])
    assert [r["base"] for r in sora["remixes"]] == ["job-1", "job-1"]


# --- one clip at a time, on one resource ----------------------------------------------- #
# A job id exists only on the resource that created it, so a mid-sequence hop to
# another endpoint leaves remix with nothing to remix. Load balancing happens
# between RUNS, never inside one.

def test_the_whole_sequence_stays_on_the_resource_that_served_shot_one(sora):
    _sequence(scenes=["a", "b", "c", "d"], continuity="remix")
    assert sora["failover"] == 1                       # only shot 1 may shop around
    assert {id(c["resource"]) for c in sora["creates"]} == {id(RESOURCE_A)}
    assert all(r["resource"] is RESOURCE_A for r in sora["remixes"])


def test_shots_are_rendered_one_at_a_time_in_order(sora):
    """Shot N+1 may need to remix N or chain from its final frame, so it cannot
    start before N finishes."""
    _sequence(scenes=["a", "b", "c"], continuity="none")
    assert [c["prompt"].count("SHOT 1 of 3") for c in sora["creates"]] == [1, 0, 0]
    assert "SHOT 3 of 3" in sora["creates"][2]["prompt"]


# --- the video is n x seconds_each, and that is the plan -------------------------------- #

def test_the_whole_video_is_the_clip_length_times_the_shot_count(sora):
    """Three 12s shots IS 36 seconds. There is no other shape available once the
    shots are chained, so the story has to be written to that total."""
    _sequence(scenes=["a", "b", "c"], continuity="remix", seconds_each=12)
    assert len(sora["creates"]) == 1 and len(sora["remixes"]) == 2


def test_a_cut_is_remixed_too(sora):
    """The source fixes the look across the jump; the prompt asks for a new
    moment. Treating a cut as "no source" is what let the cast change at a cut."""
    _sequence(scenes=["a", "b"], scene_continuity=["", "cut"])
    assert len(sora["remixes"]) == 1
    assert "ONLY to fix the look" in sora["remixes"][0]["prompt"]


def test_the_remix_source_can_go_backwards_not_only_to_the_neighbour(sora):
    """Every forward link drifts a little further; returning to an earlier shot
    is how a sequence comes back to its opening framing."""
    _sequence(scenes=["a", "b", "c", "d"], scene_remix_from=[0, 0, 1, 0])
    assert [r["base"] for r in sora["remixes"]] == ["job-1", "job-1", "remix-2"]


def test_a_forward_reference_falls_back_instead_of_failing_the_shot(sora):
    """Shot 2 cannot be edited from shot 3 — it does not exist yet."""
    result = _sequence(scenes=["a", "b", "c"], scene_remix_from=[0, 3, 0])
    assert result["clips_merged"] == 3
    assert [r["base"] for r in sora["remixes"]] == ["job-1", "remix-1"]
    assert "not available" in " ".join(result["timing_notes"])


def test_a_source_whose_shot_failed_is_not_used(monkeypatch, sora):
    """A failed shot has no job id; remixing it would take the next shot down too."""
    real_create = sequence_tool.create_clip

    async def flaky(resource, prompt, seconds, size, reference=None):
        if "SHOT 2 of 3" in prompt:
            raise RuntimeError("shot 2 is unlucky")
        return await real_create(resource, prompt, seconds, size, reference)

    monkeypatch.setattr(sequence_tool, "create_clip", flaky)
    # Every shot is created (continuity="none"), so shot 2 genuinely fails; shot 3
    # then asks to be edited from it.
    result = _sequence(scenes=["a", "b", "c"], continuity="none",
                       scene_continuity=["", "", "remix"], scene_remix_from=[0, 0, 2])
    assert result["clips_merged"] == 2
    assert [row["shot"] for row in result["failed_shots"]] == [2]
    # Shot 3 still rendered, from shot 1 rather than from the shot that failed.
    assert [r["base"] for r in sora["remixes"]] == ["job-1"]


# --- pin the cast, let the story move -------------------------------------------------- #
# Reported from a live YouTube run: a five-shot children's animation whose chain
# was PERFECT — create, remix(1), remix(2), remix(3), remix(4) — and whose
# characters changed completely anyway. The continuity clause was ordering Sora to
# keep the source clip's "location, lighting and framing exactly" while the scene
# below it asked for twilight, drizzle and a different place. A prompt at war with
# itself is settled by regenerating, and the cast is what gets regenerated.

def test_every_shot_says_the_characters_are_the_ones_in_style():
    for index in (1, 3, 5):
        prompt = build_clip_prompt("she crosses the meadow", STYLE, index=index, total=5,
                                   continues_from_frame=False)
        assert "CAST" in prompt
        assert "Do not redesign, recast, replace or add characters" in prompt


def test_the_cast_survives_a_change_of_place_and_time():
    """The whole point: the scene may move, the characters may not change."""
    prompt = build_clip_prompt("twilight on the hill, silver drizzle", STYLE, index=4,
                               total=5, continues_from_frame=False,
                               continues_from_remix=True, remix_source_shot=3)
    assert "the same characters go with it, unchanged" in prompt


def test_a_continuing_shot_no_longer_demands_the_old_light_and_place():
    """This is the contradiction that shipped: 'keep the lighting exactly' above
    a scene that asks for twilight."""
    prompt = build_clip_prompt("twilight falls", STYLE, index=2, total=3,
                               continues_from_frame=False, continues_from_remix=True)
    assert "location, lighting and framing exactly" not in prompt
    assert "Framing, location, time of day and lighting follow the shot below" in prompt
    assert "NEXT moment, not another take" in prompt        # still advances


def test_a_cut_may_land_in_another_light():
    prompt = build_clip_prompt("the same hill at night", STYLE, index=3, total=4,
                               continues_from_frame=False, continues_from_remix=True,
                               is_cut=True, remix_source_shot=2)
    assert "same characters, wardrobe, world and art style" in prompt
    assert "in whatever place, light and time of day it describes" in prompt


def test_a_single_clip_gets_no_cast_contract():
    """Nothing to be consistent WITH, and the style block already said it."""
    assert "CAST" not in build_clip_prompt("x", STYLE, index=1, total=1,
                                           continues_from_frame=False)


# --- a remix carries the source's SOUND too -------------------------------------------- #
# Reported from a live Instagram reel: scene_remix_from=[0, 1, 1], so shots 2 and 3
# were both edited from shot 1 — and all three clips spoke shot 1's sentence, over
# three visibly different scenes, each of which had been written its own Persian
# line. The scenario HAD been split correctly. Every clause in the prompt pinned
# pictures; not one of them mentioned audio, so the line came over with the frames.

def test_a_remixed_shot_is_told_not_to_reuse_the_source_clips_line():
    prompt = build_clip_prompt("she answers the phone", STYLE, index=2, total=3,
                               continues_from_frame=False, continues_from_remix=True)
    assert "AUDIO" in prompt
    assert "do not repeat, re-use or re-time any line from it" in prompt


def test_a_cut_that_is_remixed_gets_the_same_audio_contract():
    """The cut clause is the other half of the same path — [0, 1, 1] was all cuts."""
    prompt = build_clip_prompt("the empty waiting room", STYLE, index=3, total=3,
                               continues_from_frame=False, continues_from_remix=True,
                               is_cut=True, remix_source_shot=1)
    assert "PICTURE only" in prompt


def test_the_words_of_a_shot_come_only_from_that_shots_scene():
    """Which is also what makes a silent shot silent, rather than inheriting one."""
    prompt = build_clip_prompt("a hand turns the dial, no one speaks", STYLE, index=2,
                               total=3, continues_from_frame=False,
                               continues_from_remix=True)
    assert "only words spoken in this shot are the ones written in the shot below" in prompt
    assert "nobody speaks" in prompt


def test_a_shot_with_nothing_to_remix_is_not_given_an_audio_contract():
    """No source clip, no line to carry over — the clause would be noise."""
    prompt = build_clip_prompt("she crosses the meadow", STYLE, index=1, total=3,
                               continues_from_frame=False)
    assert "AUDIO" not in prompt


# --- a brief asking for a near-silent video is a direction ----------------------------- #
# Same reel: the brief said "mostly without talking or text", and the agent wrote a
# voiceover line into all three scenes AND put "Persian dialogue is spoken by a calm
# female voice" into `style` — which is repeated verbatim, so it asked for a voice in
# every clip. The video guidance presumed speech throughout and had no counterpart.

def test_the_prompt_says_a_near_silent_brief_means_no_lines():
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS as p

    assert "DECIDE HOW MUCH IS SPOKEN" in p
    assert "mostly without talking" in p
    assert "no narrator" in p.lower()


def test_the_prompt_warns_that_a_voice_in_style_speaks_in_every_clip():
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS as p

    assert "a voice described there speaks in every clip" in p


def test_the_auto_prompt_carries_the_same_direction():
    """An auto run that chooses to publish directs video from the shorter recap."""
    from aismm.agent.prompts import AUTO_INSTRUCTIONS as p

    assert "near-silent video gets shots with no lines" in p


# --- a long forward chain drifts, and the agent has to be told ------------------------- #

def test_a_long_forward_chain_is_reported_as_a_drift_risk(sora):
    """Five shots, every one remixing its neighbour, [0,0,0,0,0] — the exact
    shape of the run whose cast changed. Nothing in the result said so."""
    result = _sequence(scenes=["a", "b", "c", "d", "e"], continuity="remix")
    note = " ".join(result["timing_notes"])
    assert "generations from where the video started" in note
    assert "scene_remix_from" in note


def test_a_sequence_that_re_anchors_is_left_alone(sora):
    """[0, 0, 1, 1, 1] is the fix, so it must not also be scolded."""
    result = _sequence(scenes=["a", "b", "c", "d", "e"], continuity="remix",
                       scene_remix_from=[0, 0, 1, 1, 1])
    assert "generations from where the video started" not in " ".join(
        result.get("timing_notes", []))


def test_re_anchoring_a_sequence_too_short_to_drift_is_reported(sora):
    """The mirror of the drift warning, and the other way to get repeats.

    The live 3-shot reel planned [0, 1, 1]: shots 2 and 3 both edited from shot 1
    in a chain that was never long enough to drift, so the anchoring bought
    nothing and the two shots played as takes of one beat.
    """
    result = _sequence(scenes=["a", "b", "c"], continuity="remix",
                       scene_remix_from=[0, 1, 1])
    note = " ".join(result["timing_notes"])
    assert "chain this short does not drift" in note
    assert "takes of one beat" in note


def test_a_short_sequence_left_at_the_default_is_not_scolded(sora):
    """Consecutive chaining is the right answer here — say nothing."""
    result = _sequence(scenes=["a", "b", "c"], continuity="remix")
    assert "does not drift" not in " ".join(result.get("timing_notes", []))


def test_re_anchoring_a_long_sequence_is_left_alone(sora):
    """[0, 0, 1, 1, 1] over five shots is the documented fix, not a mistake."""
    result = _sequence(scenes=["a", "b", "c", "d", "e"], continuity="remix",
                       scene_remix_from=[0, 0, 1, 1, 1])
    assert "does not drift" not in " ".join(result.get("timing_notes", []))


def test_the_prompt_says_to_keep_the_consecutive_default(sora):
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS as p

    assert "KEEP THAT DEFAULT" in p
    assert "two takes of the same beat" in p


def test_a_short_chain_is_not_worth_a_warning(sora):
    result = _sequence(scenes=["a", "b", "c"], continuity="remix")
    assert "generations from where the video started" not in " ".join(
        result.get("timing_notes", []))


# --- faststart: browser progressive playback ----------------------------------------- #
# A freshly-encoded MP4 puts the moov atom AFTER mdat, so the dashboard <video>
# starts then stalls part-way while the player seeks back for metadata. Every
# saved video must be faststart (moov first). Needs the real ffmpeg binary.

from aismm import video as _video  # noqa: E402


def _moov_first(mp4: bytes) -> bool:
    return 0 <= mp4.find(b"moov") < mp4.find(b"mdat")


def _encode_non_faststart(seconds: int = 1) -> bytes:
    import os
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "n.mp4")
        subprocess.run([_video.ffmpeg_exe(), "-y", "-f", "lavfi", "-i",
                        f"testsrc=size=320x240:rate=24:duration={seconds}",
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", out],
                       capture_output=True)
        with open(out, "rb") as h:
            return h.read()


@pytest.mark.skipif(not _video.ffmpeg_available(), reason="ffmpeg binary not available")
def test_ensure_faststart_moves_moov_to_the_front():
    raw = _encode_non_faststart()
    assert not _moov_first(raw)  # the problem we are fixing exists in the input
    fixed = _video.ensure_faststart(raw)
    assert _moov_first(fixed)
    # Lossless remux — the clip is unchanged in length.
    assert abs(_video.duration_seconds(fixed) - _video.duration_seconds(raw)) < 0.2


def test_ensure_faststart_is_best_effort_on_bad_input():
    assert _video.ensure_faststart(b"") == b""
    assert _video.ensure_faststart(b"not-a-video") == b"not-a-video"


@pytest.mark.skipif(not _video.ffmpeg_available(), reason="ffmpeg binary not available")
def test_single_clip_concat_is_also_faststart():
    """The one-clip branch used to return the normalized clip unchanged (moov at
    the end); it must now be faststart like the multi-clip merge already is."""
    merged = _video.concat_clips([_encode_non_faststart()], "320x240")
    assert _moov_first(merged)
