"""APScheduler daemon — fires each enabled instruction on its schedule.

Schedule strings are parsed by :mod:`aismm.schedules`, which understands times of
day, weekday filters, intervals, named cadences and raw cron — combined freely
("09:00,18:00 mon-fri" or "every 6h; 08:00 mon"). One instruction can therefore
produce SEVERAL triggers, and this module registers one job per trigger.

Each fire calls :func:`aismm.orchestrator.run_instruction`; the per-account
single-flight lock prevents overlap. Call :func:`refresh_jobs` after the dashboard
adds/edits an instruction to re-sync jobs without a restart.
"""
from __future__ import annotations

import logging
import re

from apscheduler.schedulers.background import BackgroundScheduler

from .orchestrator import run_instruction
from .schedules import describe, parse_schedule, parse_trigger  # noqa: F401 - re-exported
from .store import get_store

logger = logging.getLogger("aismm.scheduler")

_scheduler: BackgroundScheduler | None = None

def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
    return _scheduler


def _job(instruction_id: str) -> None:
    logger.info("Scheduled fire for instruction %s", instruction_id)
    try:
        run_instruction(instruction_id)
    except Exception:  # noqa: BLE001 - a bad run must not kill the scheduler
        logger.exception("Scheduled run failed for instruction %s", instruction_id)


def refresh_jobs() -> None:
    """(Re)register a job for every enabled instruction with a valid schedule."""
    sched = get_scheduler()
    existing = {j.id for j in sched.get_jobs()}
    wanted: set[str] = set()
    for instr in get_store().list_instructions(enabled_only=True):
        triggers = parse_schedule(instr.schedule)
        if not triggers:
            if (instr.schedule or "").strip():
                logger.warning("Instruction '%s' has an unparseable schedule %r — it will "
                               "never fire", instr.name, instr.schedule)
            continue
        # One job per trigger: "09:00 and 18:00" is two firings, not one.
        for index, trigger in enumerate(triggers):
            job_id = f"instr:{instr.id}" if index == 0 else f"instr:{instr.id}:{index}"
            wanted.add(job_id)
            sched.add_job(_job, trigger=trigger, id=job_id, args=[instr.id],
                          replace_existing=True, misfire_grace_time=3600, coalesce=True)
        logger.info("Scheduled '%s' (%r -> %s) as %d job(s)",
                    instr.name, instr.schedule, describe(instr.schedule), len(triggers))
    # Drop jobs for instructions that were disabled/deleted.
    for job_id in existing - wanted:
        if job_id.startswith("instr:"):
            sched.remove_job(job_id)
    logger.info("Scheduler synced: %d job(s) active", len(wanted))


def start() -> BackgroundScheduler:
    sched = get_scheduler()
    if not sched.running:
        sched.start()
    refresh_jobs()
    return sched
