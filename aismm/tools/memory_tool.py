"""``read_memory`` / ``update_memory`` — the instruction's carry-over notebook.

A recurring instruction ("start at 1 Jan and work through this news site") is
useless if every run starts from zero. The agent therefore keeps a small working
memory per instruction: where it got to, what to do next, what it already
covered. It is written here, persisted by the store, and injected into the next
run's kickoff (see ``agent/prompts.build_kickoff``).

The human's standing **note** is exposed through the same read tool but is
read-only to the agent — it is the human's channel for correcting behaviour
mid-flight without rewriting the brief.

Deterministic work only (no LLM calls in tools, per the repo convention):
summarizing an overgrown memory happens in :mod:`aismm.agent.memory` after the
run, where an LLM call is allowed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from agents import function_tool

from .registry import register_tool

logger = logging.getLogger("aismm.tools.memory")

# Guard rail: one tool call can't blow the memory up beyond what the next
# kickoff can carry. The post-run compactor handles gradual growth.
MAX_MEMORY_CHARS = 20_000


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# The logic lives in plain functions (the ``perform_publish`` convention) so it is
# unit-testable without an Agents-SDK tool context.
async def perform_read_memory(state: dict) -> dict:
    """Return this instruction's carry-over memory + the operator's note."""
    record = state["store"].get_state(state["instruction"].id)
    return {
        "memory": record.memory or "(empty — this is the first run)",
        "memory_updated_at": (record.memory_updated_at.isoformat()
                              if record.memory_updated_at else None),
        "operator_note": record.note or "(none)",
        "note_updated_at": (record.note_updated_at.isoformat()
                            if record.note_updated_at else None),
    }


async def perform_update_memory(state: dict, memory: str, append: bool = False) -> dict:
    """Persist the agent-written memory, appending or replacing."""
    store = state["store"]
    instruction = state["instruction"]
    text = (memory or "").strip()
    if not text:
        return {"error": "empty_memory", "message": "Nothing to save."}

    if append:
        existing = store.get_state(instruction.id).memory
        text = f"{existing}\n\n[{_stamp()}] {text}".strip()

    truncated = False
    if len(text) > MAX_MEMORY_CHARS:
        # Keep the TAIL: the newest entries carry the current position.
        text = text[-MAX_MEMORY_CHARS:]
        truncated = True

    record = store.set_memory(instruction.id, text)
    state["memory_written"] = True
    logger.info("Memory updated for instruction %s (%d chars%s)",
                instruction.id, len(text), ", truncated" if truncated else "")
    return {"status": "saved", "chars": len(record.memory), "truncated": truncated}


def _make_read_memory(state: dict):
    @function_tool
    async def read_memory() -> dict:
        """Read this instruction's carry-over memory and the human's note.

        The memory is what YOU wrote on previous runs: how far you got, what to
        do next, and what you already covered. Read it before deciding what to
        post so you CONTINUE the work instead of repeating it.

        The note is written by the human operator and is a standing correction
        to follow (for example "prefer sources from the last 48 hours"). Treat it
        as an override of your default behaviour. You cannot edit it.
        """
        return await perform_read_memory(state)

    return read_memory


def _make_update_memory(state: dict):
    @function_tool
    async def update_memory(memory: str, append: bool = False) -> dict:
        """Save what the NEXT run needs to know. Call this before you publish.

        Write the state of the work, not a diary of this run. Keep it compact and
        factual — something like:

            CURRENT POSITION: covered news up to 2026-03-14.
            NEXT STEP: continue with 2026-03-15 onward.
            COVERED: 03-12 election piece, 03-13 budget piece, 03-14 weather.
            LEARNED: the archive paginates 20 per page; ?page=N works.

        Args:
            memory: The full replacement memory (or the text to add when
                ``append`` is true). Include the position and the next step —
                without them the next run cannot continue your work.
            append: True to add to the existing memory under a timestamp instead
                of replacing it. Use replace when you can restate the whole
                picture concisely; that keeps the memory small.
        """
        return await perform_update_memory(state, memory, append)

    return update_memory


register_tool("read_memory", _make_read_memory)
register_tool("update_memory", _make_update_memory)


# --- attachments ------------------------------------------------------------- #

async def perform_read_attachment(state: dict, filename: str) -> dict:
    """Full extracted text of one file attached to this instruction."""
    files = state.get("attachments")
    if files is None:
        files = state["store"].list_instruction_files(state["instruction"].id)
    wanted = (filename or "").strip().lower()
    match = next((f for f in files if f.filename.lower() == wanted), None)
    if match is None:
        available = ", ".join(f.filename for f in files) or "none"
        return {"error": "not_found",
                "message": f"No attachment named {filename!r}. Attached: {available}."}
    if not match.text:
        return {"filename": match.filename, "purpose": match.purpose.value,
                "content_type": match.content_type, "text": "",
                "message": ("This file has no extracted text. If it is an image, use it as a "
                            f"reference instead — asset_path: {match.asset_path}")}
    return {"filename": match.filename, "purpose": match.purpose.value,
            "content_type": match.content_type, "chars": len(match.text),
            "text": match.text}


def _make_read_attachment(state: dict):
    @function_tool
    async def read_attachment(filename: str) -> dict:
        """Read a file the human attached to this instruction, in full.

        The kickoff lists every attachment with a short excerpt; call this for the
        whole text of one (a brand guide, a PDF of source material, a price list).
        Reference *images* have no text — pass their asset_path to generate_image
        or create_video_sequence instead.

        Args:
            filename: The name shown in the attachment list.
        """
        return await perform_read_attachment(state, filename)

    return read_attachment


register_tool("read_attachment", _make_read_attachment)
