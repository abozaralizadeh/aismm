"""``finish_engagement`` — the terminal tool for an ENGAGE run.

An engagement run's job is to answer new comments/mentions, not to publish a
post, so ``publish`` is the wrong ending for it and a run that legitimately
answered three comments (or found nothing new to answer) must still be able to
end cleanly. This is that clean ending — the engage-run counterpart of
``publish`` for a post, with ``report_failure`` remaining the shared "I could not
do the job" path for both kinds of run.

The final run status reflects what the run actually did, taken from the tally
``engagement.perform_reply`` keeps on ``state["engagement"]`` (so it is recorded
in code, not from a number the model reports):

* replied to something live      -> ``published``
* staged replies (dry-run/approval) -> ``staged``
* every reply attempt was refused (403, rate limit) and nothing else landed
                                 -> ``failed`` (with the blocker reason)
* nothing new to answer          -> ``skipped``

The ``failed`` case exists because a blocked reply used to leave the tally at
``0/0/0``, so a run that tried three replies and was refused every time reported
as ``skipped`` — reading as "nothing to do" when the truth was "tried and was
blocked". ``perform_reply`` now records refusals on the tally so this can tell
the two apart.
"""
from __future__ import annotations

import logging

from agents import function_tool

from ..models import RunStatus
from .registry import register_tool

logger = logging.getLogger("aismm.tools.engagement_finish")


async def perform_finish_engagement(state: dict, summary: str = "") -> dict:
    """Record an engage run's outcome from the per-run reply tally."""
    run = state["run"]
    store = state["store"]
    instruction = state["instruction"]
    account = state["account"]
    tally = state.get("engagement") or {}
    replied = int(tally.get("replied", 0))
    staged = int(tally.get("staged", 0))
    skipped = int(tally.get("skipped", 0))
    failed = int(tally.get("failed", 0))
    failures = [f for f in (tally.get("failures") or []) if f]

    if replied:
        status = RunStatus.published
    elif staged:
        status = RunStatus.staged
    elif failed:
        # Attempts were made and the platform refused every one — that is a
        # failure, not an idle "nothing to answer" skip.
        status = RunStatus.failed
    else:
        status = RunStatus.skipped

    line = f"Engagement done: {replied} replied, {staged} staged, {skipped} skipped"
    if failed:
        line += f", {failed} blocked"
    line += "."
    if summary.strip():
        line += f" {summary.strip()}"
    run.status = status
    run.log = (run.log + "\n" + line).strip()
    # An engage run has no post caption, so the Runs list ("Caption / error")
    # would be blank for it. Use the outcome line as the run's caption so the
    # list and detail page both say what the run did without special-casing the
    # template on task_type.
    if not run.caption:
        run.caption = line
    # On a fully-blocked run, put the actual blocker in run.error so the run
    # detail says WHY it failed rather than leaving the operator to guess.
    if status is RunStatus.failed and not run.error:
        run.error = ("Every reply was refused by the platform. "
                     + " | ".join(failures[:3]))
    store.update_run(run)

    state["result"] = {"mode": "engage", "replied": replied, "staged": staged,
                       "skipped": skipped, "failed": failed, "summary": summary.strip()}
    logger.info("Engagement run finished | instruction='%s' account=%s | %s",
                instruction.name, account.handle or account.external_id, line)
    return {"status": status.value, "replied": replied, "staged": staged, "skipped": skipped,
            "failed": failed, "message": "Run recorded. This ends the engagement run."}


def _make_finish_engagement(state: dict):
    @function_tool
    async def finish_engagement(summary: str = "") -> dict:
        """End this engagement run. Call this EXACTLY ONCE, at the very end.

        Use this to finish after you have replied to (or staged replies for) the
        new comments and mentions worth answering — including when there was
        nothing new to answer, which is a normal, correct outcome. The run's
        status is set from what you actually did; you do not need to report counts.

        Do NOT use this to report a problem that stopped you from doing the job
        (the account would not load, the platform refused every read) — that is
        ``report_failure``.

        Args:
            summary: Optional one line for the run log, e.g. "answered questions
                about shipping; hid one spam comment".
        """
        return await perform_finish_engagement(state, summary)

    return finish_engagement


register_tool("finish_engagement", _make_finish_engagement)
