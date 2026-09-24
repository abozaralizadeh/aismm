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


def kickoff_block(instruction) -> str:
    """Tell the agent about a pinned style up front, so it writes shots that fit it."""
    pinned = pinned_style(instruction)
    if not pinned:
        return ""
    return ("VIDEO STYLE (set by the operator on this instruction; every video uses it "
            "verbatim as `style`, so you do not need to pass one and must not keep a copy in "
            f"memory. Write your shots to fit it):\n{pinned}\n\n")
