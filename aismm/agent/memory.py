"""Keep an instruction's carry-over memory from growing without bound.

The agent appends to its memory every run, so a daily instruction would
eventually carry a memory too large to put in a prompt. After each run we check
the size and, past the threshold, compress it with a small dedicated agent.

Why here and not in the memory tool: tools do deterministic work only (repo
convention) — an LLM call belongs in the agent layer.

The compaction prompt is deliberately conservative. Losing the *cursor* ("we got
to 14 March") breaks the entire point of the memory, so the summarizer is told to
preserve position and next-step verbatim and compress only the history behind
them.
"""
from __future__ import annotations

import logging

from agents import Agent, ModelSettings, Runner

from ..config import settings
from ..llm import build_model

logger = logging.getLogger("aismm.agent.memory")

COMPACTOR_INSTRUCTIONS = """\
You compress an AI agent's working memory for a recurring task. The memory is how
the next run knows where to continue.

Return ONLY the rewritten memory — no preamble, no commentary, no markdown fences.

MUST be preserved exactly, never summarized away:
- the CURRENT POSITION / cursor (dates, page numbers, ids, "up to X")
- the NEXT STEP
- any durable facts learned about the source (URL patterns, pagination, formats)
- operator constraints the agent recorded

MAY be compressed:
- the list of already-covered items -> collapse to ranges and counts
  ("covered 2026-03-01..2026-03-14, 14 items") keeping only the most recent few
  individually
- per-run narration, retries, and anything already implied by the position

Aim for under {target} characters. Prefer dropping old detail over losing the
position. Keep the same compact labelled shape (CURRENT POSITION / NEXT STEP /
COVERED / LEARNED).
"""


def needs_compaction(memory: str) -> bool:
    return len(memory or "") > settings.memory_max_chars


async def compact_memory(memory: str, *, model=None) -> str:
    """Summarize an oversized memory. Returns the original on any failure."""
    target = max(settings.memory_max_chars // 2, 500)
    agent = Agent(
        name="MemoryCompactor",
        instructions=COMPACTOR_INSTRUCTIONS.format(target=target),
        model=model or build_model(),
        model_settings=ModelSettings(temperature=0.2),
    )
    result = await Runner.run(agent, f"Compress this memory:\n\n{memory}", max_turns=2)
    compacted = (result.final_output or "").strip()
    if not compacted:
        raise RuntimeError("compactor returned nothing")
    return compacted


async def maybe_compact(instruction_id: str, store, *, model=None) -> bool:
    """Compact this instruction's memory if it has outgrown the limit.

    Never fatal and never destructive: if the summarizer fails, the original
    memory is left exactly as it was. ``model`` reuses the run's connection.
    """
    record = store.get_state(instruction_id)
    memory = record.memory or ""
    if not needs_compaction(memory):
        return False
    try:
        compacted = await compact_memory(memory, model=model)
    except Exception as exc:  # noqa: BLE001 - keep the run's outcome intact
        logger.warning("Memory compaction failed for %s (memory kept as-is): %s",
                       instruction_id, exc)
        return False
    if len(compacted) >= len(memory):
        logger.info("Memory compaction for %s produced no saving; keeping original",
                    instruction_id)
        return False
    store.set_memory(instruction_id, compacted, compacted=True)
    logger.info("Memory compacted for %s: %d -> %d chars",
                instruction_id, len(memory), len(compacted))
    return True
