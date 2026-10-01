"""The video STYLE lives on the instruction, where the operator can see and change it.

``style`` is the block repeated verbatim in every shot's prompt: the cast, the look, and
whatever it says about sound. It used to exist only inside the agent's tool calls and,
because briefs asked the agent to "reuse the style block", inside its MEMORY. So it was
invisible to the operator, and restrictions nobody chose travelled in it from run to run:
"Looks only: no voice, no narrator, no dialogue" sat in the children's channel's memory
long after the brief that caused it was fixed, and a psychologist reel's style gained "no
music cues" the brief never asked for. Reported as "Style should not be hidden from user,
should be accessible from the instruction".

Two fields on ``Instruction``:

* ``video_style``: written by the OPERATOR. When set it IS the style. Every video of the
  instruction uses it verbatim and the agent's own ``style`` argument is ignored, and
  told so. Code enforces this, not prompt advice, because what goes into Sora's prompt is
  exactly what the operator reads on the form.
* ``last_video_style``: written by CODE after each video. It is the style actually sent
  to Sora, so the operator can always see what the agent chose and pin it (copy it into
  ``video_style``) or correct it. It is never fed back to the agent: pinning is a human
  decision, and an auto-pinned first draft would freeze whatever that run happened to
  invent.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("aismm.video_style")

IGNORED_NOTE = ("This instruction has a Video style set by the operator, and every shot used it "
                "verbatim. The `style` you passed was not used. Put anything episode-specific "
                "(a new guest character, today's location) in the shots instead.")


def pinned_style(instruction) -> str:
    return (getattr(instruction, "video_style", "") or "").strip()


def resolve_style(state: dict, passed: str) -> tuple[str, str, str]:
    """``(style_to_use, source, note)``. ``source`` is ``"instruction"`` or ``"agent"``."""
    pinned = pinned_style(state.get("instruction"))
    passed = (passed or "").strip()
    if pinned:
        note = IGNORED_NOTE if passed and passed != pinned else ""
        return pinned, "instruction", note
    return passed, "agent", ""


def record_used_style(state: dict, style: str) -> None:
    """Show the operator the style a video actually used. Best effort, never fatal.

    The instruction is RE-READ before saving. The copy on ``state`` is as old as the run,
    and writing it back whole would silently undo any edit the operator made to the brief
    or schedule while the video rendered.
    """
    style = (style or "").strip()
    instruction, store = state.get("instruction"), state.get("store")
    if not style or instruction is None or store is None:
        return
    try:
        fresh = store.get_instruction(instruction.id)
        if fresh is None or (fresh.last_video_style or "").strip() == style:
            return
        fresh.last_video_style = style
        store.upsert_instruction(fresh)
        instruction.last_video_style = style
    except Exception as exc:  # noqa: BLE001 - a display field must never fail a video
        logger.warning("Could not record the video style used: %s", exc)


def _norm(text: str) -> str:
    return (text or "").replace("\r\n", "\n").strip()


def form_value(instruction) -> tuple[str, bool]:
    """What the instruction form's ONE Video style box shows: ``(text, pinned)``.

    Reported as "the Style used in the last video (written by the agent) is not editable;
    the user should be able to edit it for the agent". So there is a single editable box:
    the operator's pinned style when there is one, otherwise the style the agent last
    used. Edit that and save, and the agent uses the edited version from then on.
    """
    pinned = pinned_style(instruction)
    if pinned:
        return pinned, True
    return _norm(getattr(instruction, "last_video_style", "")), False


def style_from_form(submitted: str, *, shown: str | None, was_pinned: bool | None,
                    current: str) -> str:
    """The ``video_style`` to store after the form is saved.

    The box is pre-filled with the AGENT's last style when nothing is pinned, so "the
    operator saved this text" cannot mean "pin it": every unrelated save of the form would
    then freeze the agent's latest draft. Only a CHANGE pins it. The comparison is with
    what the page SHOWED (``shown``, posted back hidden), never with the current
    ``last_video_style``: a video finishing while the page was open would otherwise make
    the old, untouched text look like an edit and pin it.

    ``shown``/``was_pinned`` are ``None`` when the form did not post them (a script, an old
    page): then the submitted text is taken as written, as before.
    """
    submitted = _norm(submitted)
    if shown is None or was_pinned is None:
        return submitted
    if was_pinned:
        return submitted                   # clearing it hands the style back to the agent
    if submitted == _norm(shown):
        return current                     # untouched agent draft: stay unpinned
    return submitted                       # the operator edited it: that is now the style


def kickoff_block(instruction) -> str:
    """Tell the agent about a pinned style up front, so it writes shots that fit it."""
    pinned = pinned_style(instruction)
    if not pinned:
        return ""
    return ("VIDEO STYLE (set by the operator on this instruction; every video uses it "
            "verbatim as `style`, so you do not need to pass one and must not keep a copy in "
            f"memory. Write your shots to fit it):\n{pinned}\n\n")


# --- speech: word-less must be ASKED for -------------------------------------------- #
#
# Reported as "still all the videos are word-less even if I did not ask for it". Nothing
# stored asked for it: the kids brief, memory, note and style were clean, and on 09-24
# and 09-25 every shot had a line. On 09-26 ("How does a bell make a sound?") the agent
# chose word-free on its own, wrote "a gentle word-free story" into memory, and the next
# night copied that as if it were an instruction. The prompt now says word-less is the
# operator's call, and this is the backstop, because a rule that must hold on every run
# cannot live in model-written prose: a sequence with no spoken line anywhere is refused
# BEFORE any Sora call, unless the agent passes `wordless=True`, which is logged on the
# run so the operator sees it was a choice.

# A spoken line is QUOTED words, or an explicit narrator / voice-over. Looser signals were
# measured against every sequence of September and rejected: "Name:" also matched
# "A quiet wide view of the meadow:" and "CHARACTERS:", and speech verbs matched "what the
# record will say" and "present to speak", which are prose, not lines.
_QUOTED = re.compile(r"[\"“”«»„「『][^\"“”«»„「」『』]{2,}[\"“”«»「」『』]")
_NARRATOR = re.compile(r"(?i)\b(narrator|narrates?|narration|voice-?over)\b")
_NEGATED = re.compile(r"(?i)\b(no|without|never)\s+(spoken\s+)?(speech|dialogue|narrat\w*|"
                      r"voice-?over|voices?|words|talking|lines?)\b[^.;\n]*")


def has_spoken_line(text: str) -> bool:
    """Whether ``text`` puts words in someone's mouth: quoted words, or a narrator.

    The guard only has to catch a video with NO speech anywhere, and it runs over every
    shot plus the style, so one real line in one place is enough to let it through."""
    text = _NEGATED.sub(" ", text or "")
    return bool(_QUOTED.search(text) or _NARRATOR.search(text))


WORDLESS_REFUSAL = (
    "No shot has a spoken line, so this video would have no words at all. A video has "
    "spoken words (characters' lines or a narrator) unless the operator asked for less: "
    "write the lines into each shot's scene, in quotes, and call again. Only if the "
    "BRIEF, the OPERATOR NOTE or the Video style asks for a word-less video, call again "
    "with wordless=True. A brief that ALLOWS some words (\"at most one phrase\", \"a few "
    "words\", \"mostly without talking\") is not asking for none: use them. What an "
    "earlier video did is not a reason. Nothing was rendered or charged.")


def speech_check(state: dict, texts: list[str], wordless: bool) -> dict | None:
    """``None`` to go ahead, or the refusal to return. Logs a deliberate word-less video."""
    if any(has_spoken_line(t) for t in texts):
        return None
    if not wordless:
        return {"error": "no_spoken_lines", "message": WORDLESS_REFUSAL}
    run = state.get("run")
    if run is not None:
        run.log = ((run.log or "") + "\nWORD-LESS VIDEO: the agent set wordless=True "
                   "(no shot has a spoken line).").strip()
    return None
