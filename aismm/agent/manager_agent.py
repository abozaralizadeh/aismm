"""Build + run the autonomous manager agent for one (account, instruction) pair.

Autonomy recipe borrowed from ComicBook: one high-``max_turns`` ``Runner.run`` over
a tool-equipped agent, followed by DETERMINISTIC RECOVERY — if the model finished
without calling ``publish`` (no terminal result recorded on ``state``), we nudge it
once more with an explicit instruction to finish.
"""
from __future__ import annotations

import logging

from agents import Agent, ModelSettings, Runner

from ..llm import build_model
from ..models import Account, Instruction, Run, RunStatus
from ..platforms.registry import get_platform
from ..store.base import Store
from ..tools import build_tools
from ..tools.browse_tool import close_browser
from .memory import maybe_compact
from .prompts import MANAGER_INSTRUCTIONS, build_kickoff

logger = logging.getLogger("aismm.agent")

MAX_TURNS = 30


async def run_for_account(account: Account, instruction: Instruction, store: Store, run: Run) -> dict:
    """Run the agent once for one account. Returns the terminal result dict.

    The publish tool records the outcome on ``state["result"]`` (and persists the
    Run / StagedPost). This function returns that result (or an error dict).
    """
    caps = get_platform(account.platform).capabilities
    state: dict = {
        "account": account,
        "instruction": instruction,
        "store": store,
        "run": run,
        "assets": [],
    }
    instruction_state = store.get_state(instruction.id)
    attachments = store.list_instruction_files(instruction.id)
    state["attachments"] = attachments
    agent = Agent(
        name="SocialManager",
        instructions=MANAGER_INSTRUCTIONS,
        tools=build_tools(state),
        model=build_model(),
        model_settings=ModelSettings(temperature=0.8),
    )
    kickoff = build_kickoff(account=account, instruction=instruction,
                            platform_caps=caps, state=instruction_state,
                            files=attachments)
    # Keep the exact prompt on the Run: debugging a failure means seeing what the
    # agent was told, not what the instruction says now.
    run.prompt = kickoff
    store.update_run(run)
    logger.info("Agent ready: %d tool(s) [%s], memory=%d chars, note=%s, files=%d",
                len(agent.tools), ", ".join(getattr(t, "name", "?") for t in agent.tools),
                len(instruction_state.memory or ""),
                "yes" if (instruction_state.note or "").strip() else "no",
                len(attachments))

    try:
        result = await Runner.run(agent, kickoff, max_turns=MAX_TURNS)

        # --- deterministic recovery: ensure the run reached a terminal publish ---
        if not state.get("result"):
            assets = state.get("assets", [])
            asset_hint = (
                f"You already have media at: {assets[-1]['path']} "
                f"(kind={assets[-1]['kind']}) — use it if it fits the brief."
                if assets else ""
            )
            memory_hint = (
                "" if state.get("memory_written")
                else "Also call update_memory with where you got to. "
            )
            nudge = (
                "You did not finish this run. " + memory_hint +
                "End it now with exactly one terminal call:\n"
                "- publish, IF you have a real post that satisfies the brief. "
                + asset_hint +
                "\n- report_failure, if you could not carry out the instruction. "
                "Do NOT publish a post that describes the problem, apologises, or "
                "substitutes invented content for what you failed to fetch — a "
                "failed run is the correct outcome there."
            )
            logger.info("Recovery nudge for account=%s instruction=%s", account.id, instruction.id)
            # Continue the SAME conversation so prior tool outputs/assets are retained.
            follow_up = result.to_input_list() + [{"role": "user", "content": nudge}]
            await Runner.run(agent, follow_up, max_turns=8)
    finally:
        # Tear the browser down inside THIS event loop (the AIBlog lesson): a
        # Chromium subprocess finalized later by GC raises "Event loop is closed".
        await close_browser(state)

    if not state.get("memory_written"):
        logger.warning("Agent did NOT update memory for instruction %s — the next run "
                       "will not know where this one got to", instruction.id)
    # Summarize an overgrown memory now, so the next kickoff stays small. Never
    # fatal — a failed compaction leaves the memory untouched.
    await maybe_compact(instruction.id, store)

    if state.get("result"):
        return state["result"]

    # Neither terminal tool was called even after the nudge. That is a failed
    # run, recorded as one — not a silent no-op the operator never sees.
    message = ("The agent ended without calling publish or report_failure. "
               "Check the tool errors above for what blocked it.")
    logger.error("Run ended with no terminal call | instruction='%s' account=%s",
                 instruction.name, account.handle or account.external_id)
    run.status = RunStatus.failed
    run.error = message
    run.log = (run.log + "\nFAILED: no terminal tool call.").strip()
    store.update_run(run)
    return {"error": "no_terminal_call", "message": message}
