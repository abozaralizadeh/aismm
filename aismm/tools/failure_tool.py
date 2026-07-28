"""``report_failure`` — end a run WITHOUT publishing.

A social account is not a scratchpad. If the agent cannot carry out the
instruction — the source page won't load, there is nothing new to post, every
media attempt failed — the right outcome is a failed run with a diagnostic
trail, not a post explaining the difficulty to the account's followers.

Before this tool existed the agent had no way to say "I can't", and the prompt
told it to publish regardless, so a blocked run would invent something and ship
it. This is the other terminal path alongside ``publish``: both end the run, and
exactly one of them should be called.

The reason lands in the ``Run`` record, so it shows up in the dashboard's Runs
table and in the service log next to the tool errors that caused it.
"""
from __future__ import annotations

import logging

from agents import function_tool

from ..models import RunStatus
from .registry import register_tool

logger = logging.getLogger("aismm.tools.failure")


async def perform_report_failure(state: dict, reason: str, details: str = "",
                                 next_step: str = "") -> dict:
    """Record a run as failed with the agent's own diagnosis."""
    run = state["run"]
    store = state["store"]
    instruction = state["instruction"]
    account = state["account"]

    summary = (reason or "").strip() or "The agent reported a failure with no reason given."
    parts = [f"FAILED: {summary}"]
    if details.strip():
        parts.append(f"Details: {details.strip()}")
    if next_step.strip():
        parts.append(f"Suggested next step: {next_step.strip()}")
    message = "\n".join(parts)

    run.status = RunStatus.failed
    run.error = summary
    run.log = (run.log + "\n" + message).strip()
    store.update_run(run)

    state["result"] = {"mode": "failed", "reason": summary, "details": details.strip(),
                       "next_step": next_step.strip()}
    logger.error("Agent reported failure | instruction='%s' account=%s | %s",
                 instruction.name, account.handle or account.external_id,
                 message.replace("\n", " | "))
    return {"status": "failed",
            "message": "Run recorded as failed. Nothing was published — this is the correct "
                       "outcome when the instruction cannot be carried out."}


def _make_report_failure(state: dict):
    @function_tool
    async def report_failure(reason: str, details: str = "", next_step: str = "") -> dict:
        """End this run WITHOUT posting, because you cannot carry out the instruction.

        Call this instead of ``publish`` when the work could not be done — for
        example the page you were told to read did not load, the content you
        needed was not there, there is nothing new since the last run, or every
        attempt to produce the required media failed.

        NEVER publish a post that talks about the problem. A caption explaining
        that something went wrong is not a post; it is a bug report, and it goes
        to real followers. Use this tool instead: it is recorded as a failed run
        and shown to the operator with your reason.

        Args:
            reason: One line on what stopped you ("the comic page returned no
                panel images for 2026-05-13").
            details: What you tried and what came back — tool names, URLs, error
                messages. This is what the operator debugs from, so be specific.
            next_step: Optional. What should happen next, or what would fix it
                ("retry once the page renders panels", "skip to the next date").
        """
        return await perform_report_failure(state, reason, details, next_step)

    return report_failure


register_tool("report_failure", _make_report_failure)
