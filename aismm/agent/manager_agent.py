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
from ..config import SoraSettings, settings
from ..llm import build_model_for
from ..llm_access import can_select, env_config, env_provider_config
from ..models import (
    ENV_IMAGE_ID, ENV_LLM_ID, ENV_VIDEO_ID, Account, Instruction, InstructionTask, Run,
    RunStatus,
)
from ..platforms.registry import get_platform
from ..store.base import Store
from ..tools import build_tools, sora_config
from ..tools.browse_tool import close_browser
from .memory import maybe_compact
from .prompts import (AUTO_INSTRUCTIONS, ENGAGEMENT_INSTRUCTIONS, MANAGER_INSTRUCTIONS,
                      OUTREACH_INSTRUCTIONS, build_auto_kickoff, build_engagement_kickoff,
                      build_kickoff, build_outreach_kickoff, build_performance_block)

logger = logging.getLogger("aismm.agent")

MAX_TURNS = 30


def _performance_block(store: Store, account: Account) -> str:
    """Render the "how recent posts did" kickoff section for this account.

    Best-effort: the feedback loop is a helpful nudge, never a precondition for a
    run, so any store hiccup yields no block rather than failing the run.
    """
    try:
        from ..tools.performance_tool import recent_performance_runs

        return build_performance_block(recent_performance_runs(store, account.id))
    except Exception as exc:  # noqa: BLE001 - a metrics read must never block a run
        logger.warning("Could not build the performance summary for %s: %s",
                       account.handle or account.external_id, exc)
        return ""


_NO_LLM_MESSAGE = (
    "No LLM connection is available for this instruction. Add one in "
    "Settings → LLM connections and select it, or ask the owner to share the "
    "deployment model with you."
)


def _resolve_model(instruction: Instruction, store: Store):
    """Return the Responses model this instruction may use, or ``None``.

    ``None`` means no accessible LLM — the run must fail with a clear message
    rather than silently falling back to the deployment default (the owner
    controls who may use it).
    """
    cfg_id = instruction.llm_config_id or ENV_LLM_ID
    cfg = store.get_llm_config(cfg_id)
    if cfg is None:
        # The env sentinel resolves even before its row is created; any other
        # missing id means the connection was deleted — no fallback.
        if cfg_id != ENV_LLM_ID:
            return None
        cfg = env_config()
    # A scheduled run acts on behalf of the instruction's workspace: the config
    # must be owned by / shared with that workspace or its creator. SSO-off means
    # a single local operator who owns everything.
    workspace = store.get_workspace(instruction.workspace_id)
    creator = workspace.created_by if workspace else ""
    is_owner = (not settings.auth.enabled) or settings.auth.is_owner(creator)
    member_ids = {instruction.workspace_id, ""}
    if not can_select(cfg, creator, member_ids, is_owner=is_owner):
        return None
    llm = store.resolve_llm_settings(cfg_id)
    if llm is None:
        return None
    return build_model_for(llm)


def _resolve_provider(instruction: Instruction, store: Store, kind: str):
    """Resolve the image/video (Sora) connection this instruction may use, or ``None``.

    Mirrors ``_resolve_model``'s access gate, but image/video are OPTIONAL: ``None``
    just leaves the generation tool absent (an unconfigured, deleted or inaccessible
    connection is not a run failure — unlike the mandatory LLM). Empty selection
    resolves to the deployment ``.env`` sentinel, gated like any other connection.
    """
    env_id = ENV_IMAGE_ID if kind == "image" else ENV_VIDEO_ID
    selected = (instruction.image_config_id if kind == "image"
                else instruction.video_config_id)
    cfg_id = selected or env_id
    cfg = store.get_provider_config(cfg_id)
    if cfg is None:
        # The env sentinel resolves even before its row exists; any other missing
        # id means the connection was deleted — no fallback, just disable the tool.
        if cfg_id != env_id:
            return None
        cfg = env_provider_config(kind)
    workspace = store.get_workspace(instruction.workspace_id)
    creator = workspace.created_by if workspace else ""
    is_owner = (not settings.auth.enabled) or settings.auth.is_owner(creator)
    member_ids = {instruction.workspace_id, ""}
    if not can_select(cfg, creator, member_ids, is_owner=is_owner):
        return None
    return (store.resolve_image_settings(cfg_id) if kind == "image"
            else store.resolve_sora_settings(cfg_id))


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
    outreach = task is InstructionTask.outreach
    auto = task is InstructionTask.auto
    state: dict = {
        "account": account,
        "instruction": instruction,
        "store": store,
        "run": run,
        "assets": [],
    }
    # Resolve the model this instruction may use BEFORE any work. No accessible
    # LLM is a clear failure, never a silent fallback to the deployment default
    # (the owner controls who may use it).
    model = _resolve_model(instruction, store)
    if model is None:
        logger.error("No accessible LLM for instruction '%s' (config=%r) — failing run",
                     instruction.name, instruction.llm_config_id or ENV_LLM_ID)
        run.status = RunStatus.failed
        run.error = _NO_LLM_MESSAGE
        run.log = (run.log + "\nFAILED: no LLM connection available.").strip()
        store.update_run(run)
        return {"error": "no_llm", "message": _NO_LLM_MESSAGE}
    state["model"] = model
    # Image/video connections are OPTIONAL — None just disables the generation
    # tool. Resolve BEFORE build_tools (which gates those tools) and make the Sora
    # pool active for the whole run via the ContextVar; an empty SoraSettings when
    # None means "no accessible video connection", so the Sora tools stay off
    # rather than silently falling back to the .env pool. Reset in the finally.
    state["image_settings"] = _resolve_provider(instruction, store, "image")
    sora_settings = _resolve_provider(instruction, store, "video")
    state["sora_settings"] = sora_settings
    sora_token = sora_config._ACTIVE.set(
        sora_settings if sora_settings is not None else SoraSettings())
    instruction_state = store.get_state(instruction.id)
    attachments = store.list_instruction_files(instruction.id)
    state["attachments"] = attachments
    if auto:
        agent_name, instructions, build = "SocialManager", AUTO_INSTRUCTIONS, build_auto_kickoff
    elif engage:
        agent_name, instructions, build = ("SocialEngager", ENGAGEMENT_INSTRUCTIONS,
                                           build_engagement_kickoff)
    elif outreach:
        agent_name, instructions, build = ("SocialOutreach", OUTREACH_INSTRUCTIONS,
                                           build_outreach_kickoff)
    else:
        agent_name, instructions, build = "SocialManager", MANAGER_INSTRUCTIONS, build_kickoff
    agent = Agent(
        name=agent_name,
        instructions=instructions,
        tools=build_tools(state, instruction.tools),
        model=model,
        model_settings=ModelSettings(temperature=0.8),
    )
    if prompt_override.strip():
        # A retry sends exactly what the operator edited — no memory, note or
        # performance is re-inlined, so what they read in the box is what runs.
        kickoff = prompt_override.strip()
    else:
        build_kwargs = dict(account=account, instruction=instruction,
                            platform_caps=caps, state=instruction_state, files=attachments)
        # Close the feedback loop: a run that may PUBLISH sees how its recent posts
        # performed, from turn one. Engage and outreach runs post nothing, so they
        # are skipped (and their kickoff builders take no performance kwarg).
        if not (engage or outreach):
            build_kwargs["performance"] = _performance_block(store, account)
        kickoff = build(**build_kwargs)
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
            elif outreach:
                nudge = (
                    "You did not finish this run. " + memory_hint +
                    "End it now with exactly one terminal call:\n"
                    "- finish_engagement, once you have engaged (or staged replies for) the "
                    "other accounts' posts worth engaging — including when you found nothing "
                    "worth engaging, which is a normal, correct outcome.\n"
                    "- report_failure, only if something stopped you from doing the job at "
                    "all (search would not run, every read was refused)."
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
        # Drop the per-run Sora pool so it never bleeds into the next run's context.
        sora_config._ACTIVE.reset(sora_token)

    if not state.get("memory_written"):
        logger.warning("Agent did NOT update memory for instruction %s — the next run "
                       "will not know where this one got to", instruction.id)
    # Summarize an overgrown memory now, so the next kickoff stays small. Never
    # fatal — a failed compaction leaves the memory untouched.
    await maybe_compact(instruction.id, store, model=model)

    if state.get("result"):
        return state["result"]

    # Neither terminal tool was called even after the nudge. That is a failed
    # run, recorded as one — not a silent no-op the operator never sees.
    if auto:
        ending = "publish, finish_engagement or report_failure"
    elif engage or outreach:
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
