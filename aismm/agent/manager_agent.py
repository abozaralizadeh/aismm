"""Build + run the autonomous manager agent for one (account, instruction) pair.

Autonomy recipe borrowed from ComicBook: one high-``max_turns`` ``Runner.run`` over
a tool-equipped agent, followed by DETERMINISTIC RECOVERY — if the model finished
without calling ``publish`` (no terminal result recorded on ``state``), we nudge it
once more with an explicit instruction to finish.
"""
from __future__ import annotations

import logging

from agents import Agent, ModelSettings, Runner

from ..attachments import build_agent_input, looks_like_unsupported_file_input
from ..llm import build_model
from ..models import Account, Instruction, InstructionTask, Run, RunStatus
from ..platforms.registry import get_platform
from ..store.base import Store
from ..tools import build_tools
from ..tools.browse_tool import close_browser
from .memory import maybe_compact
from .prompts import (AUTO_INSTRUCTIONS, ENGAGEMENT_INSTRUCTIONS, MANAGER_INSTRUCTIONS,
                      build_auto_kickoff, build_engagement_kickoff, build_kickoff)

logger = logging.getLogger("aismm.agent")

MAX_TURNS = 30


async def run_for_account(account: Account, instruction: Instruction, store: Store, run: Run,
                          prompt_override: str = "") -> dict:
    """Run the agent once for one account. Returns the terminal result dict.

    The publish tool records the outcome on ``state["result"]`` (and persists the
    Run / StagedPost). This function returns that result (or an error dict).

    ``prompt_override`` replaces the composed kickoff verbatim — the dashboard's
    retry, where an operator re-sends a failed run's prompt after editing it.
    Memory and the operator note are NOT re-inlined in that case: the point is to
    send exactly what is in the box, so what you read is what the agent gets.
    """
    caps = get_platform(account.platform).capabilities
    task = instruction.task_type
    engage = task is InstructionTask.engage
    auto = task is InstructionTask.auto
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
    if auto:
        agent_name, instructions, build = "SocialManager", AUTO_INSTRUCTIONS, build_auto_kickoff
    elif engage:
        agent_name, instructions, build = ("SocialEngager", ENGAGEMENT_INSTRUCTIONS,
                                           build_engagement_kickoff)
    else:
        agent_name, instructions, build = "SocialManager", MANAGER_INSTRUCTIONS, build_kickoff
    agent = Agent(
        name=agent_name,
        instructions=instructions,
        tools=build_tools(state, instruction.tools),
        model=build_model(),
        model_settings=ModelSettings(temperature=0.8),
    )
    kickoff = (prompt_override.strip() or
               build(account=account, instruction=instruction,
                     platform_caps=caps, state=instruction_state,
                     files=attachments))
    # Keep the exact prompt on the Run: debugging a failure means seeing what the
    # agent was told, not what the instruction says now. run.prompt always stays
    # plain text even when the model also receives files natively.
    run.prompt = kickoff
    store.update_run(run)
    agent_input, attached_natively, fell_back = build_agent_input(kickoff, attachments)
    logger.info("Agent ready: %d tool(s) [%s], memory=%d chars, note=%s, files=%d "
                "(%d attached natively, %d as text)",
                len(agent.tools), ", ".join(getattr(t, "name", "?") for t in agent.tools),
                len(instruction_state.memory or ""),
                "yes" if (instruction_state.note or "").strip() else "no",
                len(attachments), len(attached_natively), len(fell_back))
    if attached_natively:
        logger.info("Attached natively to the model: %s", ", ".join(attached_natively))
    if fell_back:
        logger.info("Too large/unreadable to attach natively, using extracted text: %s",
                    ", ".join(fell_back))

    try:
        try:
            result = await Runner.run(agent, agent_input, max_turns=MAX_TURNS)
        except Exception as exc:  # noqa: BLE001 - only retry the specific failure we can fix
            if isinstance(agent_input, list) and looks_like_unsupported_file_input(str(exc)):
                logger.warning("Deployment rejected native file input (%s) — retrying as "
                               "text-only", exc)
                result = await Runner.run(agent, kickoff, max_turns=MAX_TURNS)
            else:
                raise

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
            if auto:
                nudge = (
                    "You did not finish this run. " + memory_hint +
                    "End it now with exactly one terminal call, matching the ONE job you "
                    "did this run:\n"
                    "- publish, IF you produced a real post that satisfies the brief. "
                    + asset_hint +
                    "\n- finish_engagement, IF you were replying to comments/mentions — "
                    "including when there was nothing new to answer.\n"
                    "- report_failure, only if something stopped you from doing either job "
                    "at all. Do NOT publish a post that describes a problem or invents "
                    "content you could not fetch."
                )
            elif engage:
                nudge = (
                    "You did not finish this run. " + memory_hint +
                    "End it now with exactly one terminal call:\n"
                    "- finish_engagement, once you have replied to (or staged replies for) "
                    "the new comments/mentions worth answering — including when there was "
                    "nothing new to answer, which is a normal, correct outcome.\n"
                    "- report_failure, only if something stopped you from doing the job at "
                    "all (the account would not load, every read was refused)."
                )
            else:
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
    if auto:
        ending = "publish, finish_engagement or report_failure"
    elif engage:
        ending = "finish_engagement or report_failure"
    else:
        ending = "publish or report_failure"
    message = (f"The agent ended without calling {ending}. "
               "Check the tool errors above for what blocked it.")
    logger.error("Run ended with no terminal call | instruction='%s' account=%s",
                 instruction.name, account.handle or account.external_id)
    run.status = RunStatus.failed
    run.error = message
    run.log = (run.log + "\nFAILED: no terminal tool call.").strip()
    store.update_run(run)
    return {"error": "no_terminal_call", "message": message}
