"""A video has spoken words unless the OPERATOR asked for less.

Reported as "still all the videos are word-less even if I did not ask for it". Checked
against everything stored: no brief, note, memory or style of the kids channel asked for
silence, and on 09-24/09-25 every shot had a line. On 09-26 the agent chose "word-free"
itself, wrote that into memory, and the next night copied it. Measured over every video
sequence of September: 19 of 28 had no spoken line anywhere. Pinned here:

* the detector counts QUOTED words or an explicit narrator, and nothing looser (the real
  false positives that ruled out "Name:" and speech verbs are kept as cases);
* a sequence or clip with no spoken line anywhere is refused BEFORE any Sora call;
* ``wordless=True`` lets it through and says so on the run log;
* a refusal is not a video failure (it must not trip the circuit breaker);
* the prompt says word-less is the operator's decision and memory is not a reason.
"""
import asyncio
import json

import pytest

from aismm.agent import prompts
from aismm.models import Instruction, Run
from aismm.video_style import WORDLESS_REFUSAL, has_spoken_line


def _invoke(tool, **kwargs):
    from agents import RunConfig
    from agents.tool_context import ToolContext

    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="1",
                      tool_arguments="{}", run_config=RunConfig())
    out = asyncio.run(tool.on_invoke_tool(ctx, json.dumps(kwargs)))
    return json.loads(out) if isinstance(out, str) else out


@pytest.mark.parametrize("text", [
    'Humi says, “The pebble sinks to the bottom.”',                 # 09-24 kids, real
    'Pip whispers "shh" and points.',
    "the patient says «هَر صِدایی بَرایِ هَمِه یِکْسان نیست»",       # the Audiology phrase
    "A warm narrator explains why the leaf floats.",
    "Voice-over: sound is air that wiggles.",
])
def test_real_lines_count(text):
    assert has_spoken_line(text)


@pytest.mark.parametrize("text", [
    # 09-26/09-27 kids, real: the agent wrote "no speech" into every shot
    "Pupu, Pip, Humi and the little snail notice a golden bell. Quiet stream ambience, "
    "no speech",
    "Looks only: no voice, no narrator, no dialogue, nothing loud.",
    "No narration, no subtitles, no on-screen text.",
    # the loose signals that were measured and rejected
    "A quiet wide view of the meadow: Pupu, Pip and Humi notice their soft shadows.",
    "CHARACTERS: Agnes Vale, adult woman, short wavy dark hair.",
    "Linh holds the ledger close as she weighs what the record will say.",
    "the missing groom is finally present to speak, and the group breathes.",
])
def test_prose_without_a_line_does_not_count(text):
    assert not has_spoken_line(text)


# --- the tools ------------------------------------------------------------------------ #

def _sequence_tool(monkeypatch, state, calls):
    from aismm.tools import sequence_tool

    async def fake_perform(st, scenes, *, style, **_kw):
        calls.append(scenes)
        return {"asset_path": "x.mp4"}

    monkeypatch.setattr(sequence_tool, "perform_create_sequence", fake_perform)
    monkeypatch.setattr(sequence_tool.sora_config, "enabled", lambda: True)
    return sequence_tool._make_create_sequence(state)


def test_a_wordless_sequence_is_refused_before_any_sora_call(monkeypatch):
    calls = []
    state = {"instruction": Instruction(name="I")}
    tool = _sequence_tool(monkeypatch, state, calls)
    out = _invoke(tool, scenes=["The bell rings. no speech", "They listen quietly."],
                  style="Soft watercolour.")
    assert out["error"] == "no_spoken_lines" and out["message"] == WORDLESS_REFUSAL
    assert calls == [], "the refusal must come before any money is spent"
    assert state.get("video_failures", 0) == 0, "a refusal is not a video failure"


def test_one_line_in_one_shot_is_enough(monkeypatch):
    calls = []
    tool = _sequence_tool(monkeypatch, {"instruction": Instruction(name="I")}, calls)
    out = _invoke(tool, scenes=["The bell rings.", 'Pip says, "Ding!"', "They listen."])
    assert out.get("asset_path") == "x.mp4" and len(calls) == 1


def test_wordless_true_goes_ahead_and_is_logged_on_the_run(monkeypatch):
    calls = []
    run = Run(instruction_id="i", account_id="a", log="started")
    tool = _sequence_tool(monkeypatch, {"instruction": Instruction(name="I"), "run": run},
                          calls)
    out = _invoke(tool, scenes=["The bell rings.", "They listen."], wordless=True)
    assert out.get("asset_path") == "x.mp4"
    assert "WORD-LESS VIDEO" in run.log


def test_a_narrator_in_a_pinned_video_style_counts(monkeypatch):
    calls = []
    instr = Instruction(name="I", video_style="Soft watercolour. A warm narrator tells it.")
    tool = _sequence_tool(monkeypatch, {"instruction": instr}, calls)
    out = _invoke(tool, scenes=["The bell rings.", "They listen."])
    assert out.get("asset_path") == "x.mp4"


def test_generate_video_is_checked_too(monkeypatch):
    from aismm.tools import video_tool

    seen = []

    async def fake_perform(state, prompt, **_kw):
        seen.append(prompt)
        return {"asset_path": "x.mp4"}

    monkeypatch.setattr(video_tool, "perform_generate_video", fake_perform)
    monkeypatch.setattr(video_tool.sora_config, "enabled", lambda: True)
    tool = video_tool._make_generate_video({"instruction": Instruction(name="I")})
    assert _invoke(tool, prompt="A bell rings in a meadow.")["error"] == "no_spoken_lines"
    assert seen == []
    assert _invoke(tool, prompt="A bell rings.", wordless=True)["asset_path"] == "x.mp4"


# --- the prompt ----------------------------------------------------------------------- #

def test_the_prompt_makes_wordless_the_operators_decision():
    text = " ".join(prompts.MANAGER_INSTRUCTIONS.split())
    assert "A video HAS SPOKEN WORDS" in text
    assert "Going word-less is the OPERATOR's decision, never yours" in text
    assert "what you did last time is not an instruction" in text


def test_a_brief_allowing_one_phrase_is_not_a_wordless_request():
    """The 10-01 psychologist reel: "at most one phrase!" came out with zero words."""
    assert "at most one phrase" in WORDLESS_REFUSAL and "use them" in WORDLESS_REFUSAL
    text = " ".join(prompts.MANAGER_INSTRUCTIONS.split())
    assert "means the video HAS that one phrase" in text
