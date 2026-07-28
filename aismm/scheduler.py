"""APScheduler daemon — fires each enabled instruction on its schedule.

Schedule strings accept either:
  * cron    — a 5-field crontab expression, e.g. ``0 9 * * *`` (09:00 daily), or
  * interval — ``every 6h`` / ``6h`` / ``30m`` / ``every 2 hours`` / ``1d``.

Each fire calls :func:`aismm.orchestrator.run_instruction`; the per-account
single-flight lock prevents overlap. Call :func:`refresh_jobs` after the dashboard
adds/edits an instruction to re-sync jobs without a restart.
"""
from __future__ import annotations

import logging
import re

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .orchestrator import run_instruction
from .store import get_store

logger = logging.getLogger("aismm.scheduler")

_scheduler: BackgroundScheduler | None = None

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_WORD_UNIT = {"second": "s", "minute": "m", "hour": "h", "day": "d"}


def parse_trigger(schedule: str):
    """Turn a schedule string into an APScheduler trigger (or None if invalid)."""
    s = (schedule or "").strip().lower()
    if not s:
        return None

    # interval: "every 6h", "every 2 hours", "6h", "30m", "1d"
    m = re.match(r"^(?:every\s+)?(\d+)\s*([smhd]|seconds?|minutes?|hours?|days?)$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        unit = unit[0] if unit in _UNIT_SECONDS else _WORD_UNIT.get(unit.rstrip("s"), "h")
        seconds = n * _UNIT_SECONDS.get(unit, 3600)
        return IntervalTrigger(seconds=max(seconds, 60))

    # cron: 5 whitespace-separated fields
    if len(s.split()) == 5:
        try:
            return CronTrigger.from_crontab(schedule.strip())
        except ValueError as exc:
            logger.warning("Invalid cron '%s': %s", schedule, exc)
            return None

    logger.warning("Unrecognized schedule '%s' (use cron or 'every Nh')", schedule)
    return None


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
        trigger = parse_trigger(instr.schedule)
        if trigger is None:
            continue
        job_id = f"instr:{instr.id}"
        wanted.add(job_id)
        sched.add_job(_job, trigger=trigger, id=job_id, args=[instr.id],
                      replace_existing=True, misfire_grace_time=3600, coalesce=True)
        logger.info("Scheduled '%s' (%s) -> %s", instr.name, instr.schedule, trigger)
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
