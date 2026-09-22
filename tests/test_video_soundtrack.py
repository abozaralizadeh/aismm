"""What a video SOUNDS like is decided by the instruction, never by our prompt or code.

Reported as "why are all the recent videos silent?". The files had audio (60s of
stream ambience at about -18 dB). The brief asked for almost no speech and never
mentioned music, and our own guidance had quietly filled that gap with "no sound":
the remix contract said "ambient sound only" and the prompt said "a shot you give no
line to is silent". A first fix swung the other way, telling the agent to add music
and injecting a soundtrack line from code. The operator's correction: music is a
creative choice and belongs in the brief. So the rule pinned here is neutrality. The
tool only stops the source clip's WORDS carrying over, and says nothing that adds or
rules out music.
"""
from aismm.agent import prompts
from aismm.tools import sequence_tool
from aismm.tools.sequence_tool import _AUDIO_CONTRACT, build_clip_prompt

STYLE = ("Stylised preschool cartoon animals, cozy meadow beside a stream. No voice, "
         "no narrator.")
SHOT = "The snail crosses the pebble bridge. No speech."


def _text(s: str) -> str:
    return " ".join(s.split()).lower()


def test_the_remix_contract_still_stops_the_source_clips_words():
    assert "do NOT carry over" in _AUDIO_CONTRACT
    assert "only words spoken in this shot" in _AUDIO_CONTRACT


def test_the_remix_contract_decides_nothing_about_music():
    text = _text(_AUDIO_CONTRACT)
    assert "ambient sound only" not in text     # ruled music out
    assert "music" not in text                  # and must not rule it in either


def test_code_adds_no_sound_of_its_own():
    """The prompt Sora gets is style + contracts + the scene, nothing about sound."""
    for kwargs in ({}, {"continues_from_remix": True}, {"from_supplied_image": True}):
        prompt = build_clip_prompt(SHOT, STYLE, index=2, total=5,
                                   continues_from_frame=False, **kwargs)
        for word in ("music", "soundtrack", "sound effect"):
            assert word not in prompt.lower(), (kwargs, word)
    assert not hasattr(sequence_tool, "_SOUNDTRACK_FALLBACK")


def test_the_prompt_sends_sound_decisions_to_the_brief():
    text = _text(prompts.MANAGER_INSTRUCTIONS)
    assert "a shot you give no line to is silent" not in text
    assert "that says nothing about music or sound effects: those follow the brief" in text
    assert "do not add or rule out what it does not mention" in text
    # It must not impose a house default either way.
    assert "a quiet video still has music" not in text
    assert "only a brief that explicitly says" not in text


def test_the_prompt_still_honours_the_briefs_exact_speech_allowance():
    text = _text(prompts.MANAGER_INSTRUCTIONS)
    assert "one shot gets a few words" in text
