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
import threading
from datetime import datetime

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .orchestrator import run_instruction
from .schedules import describe, parse_schedule, parse_trigger  # noqa: F401 - re-exported
from .store import get_store

logger = logging.getLogger("aismm.scheduler")

_scheduler: BackgroundScheduler | None = None

# A run occupies one pool thread for as long as it takes (minutes). The default
# pool of 10 is enough for a handful of instructions, but it is worth being
# explicit: when every thread is busy, jobs do not fail — they are skipped, and
# for a long time that happened with no line in our log to explain it.
MAX_CONCURRENT_RUNS = 12
# One run per instruction at a time. The per-account lock enforces this anyway;
# saying it here means APScheduler skips the fire instead of queueing a run that
# would immediately be refused by the lock.
MAX_INSTANCES = 1


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(
            timezone="UTC",
            executors={"default": ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS)},
            job_defaults={"max_instances": MAX_INSTANCES, "coalesce": True,
                          "misfire_grace_time": 3600},
        )
        _scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)
    return _scheduler


def _on_job_missed(event) -> None:
    """Say out loud when a fire was dropped, and why.

    A skipped fire is the symptom of a run that never finished — with
    ``max_instances=1`` one wedged run silences its instruction indefinitely.
    Without this, the instruction just quietly stops posting.
    """
    if event.code == EVENT_JOB_MAX_INSTANCES:
        logger.warning("Instruction job %s did NOT fire: its previous run is still going. "
                       "If this repeats, that run is wedged — check for a RUN START with no "
                       "matching RUN DONE.", event.job_id)
    else:
        logger.warning("Instruction job %s missed its scheduled time (%s) — the scheduler was "
                       "busy or the process was down.", event.job_id, event.scheduled_run_time)


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
        # An interval trigger anchors to the moment it is CONSTRUCTED unless told
        # otherwise, and this loop reconstructs every trigger on every refresh
        # (dashboard save of any instruction, service restart). Anchoring to a
        # stable point — the operator's explicit start, else created_at — is what
        # keeps "every 1h" from silently re-basing its phase each time.
        anchor = instr.schedule_start_at or instr.created_at
        triggers = parse_schedule(instr.schedule, anchor=anchor)
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
                          replace_existing=True, misfire_grace_time=3600, coalesce=True,
                          max_instances=MAX_INSTANCES)
        logger.info("Scheduled '%s' (%r -> %s) as %d job(s)", instr.name, instr.schedule,
                    describe(instr.schedule, starts_at=instr.schedule_start_at), len(triggers))
    # Drop jobs for instructions that were disabled/deleted.
    for job_id in existing - wanted:
        if job_id.startswith("instr:"):
            sched.remove_job(job_id)
    logger.info("Scheduler synced: %d job(s) active", len(wanted))


def next_run_for(instruction_id: str) -> datetime | None:
    """When this instruction's job(s) will next fire, for the dashboard.

    An instruction can hold several jobs (one per trigger — "09:00 and 18:00" is
    two), so this is the earliest of them. Live scheduler state, not persisted —
    ``None`` when the scheduler isn't running here (dashboard-only mode) or the
    instruction has no valid schedule.
    """
    sched = get_scheduler()
    if not sched.running:
        return None
    prefix = f"instr:{instruction_id}"
    times = [j.next_run_time for j in sched.get_jobs()
            if j.id == prefix or j.id.startswith(f"{prefix}:")]
    times = [t for t in times if t]
    return min(times) if times else None


def next_run_after(instruction_id: str, after: datetime) -> datetime | None:
    """First fire of this instruction at or after ``after``.

    Used to answer "when will something actually happen?" when the next scheduled
    fire is going to be skipped — a rate-limited account, say. Asks the triggers
    directly rather than the jobs' cached ``next_run_time``, which only knows
    about the very next one.
    """
    sched = get_scheduler()
    if not sched.running:
        return None
    prefix = f"instr:{instruction_id}"
    times = []
    for job in sched.get_jobs():
        if job.id != prefix and not job.id.startswith(f"{prefix}:"):
            continue
        fire = job.trigger.get_next_fire_time(None, after)
        if fire:
            times.append(fire)
    return min(times) if times else None


def start() -> BackgroundScheduler:
    sched = get_scheduler()
    if not sched.running:
        sched.start()
    _reap_stale_runs()
    _schedule_housekeeping(sched)
    refresh_jobs()
    return sched


def _schedule_housekeeping(sched) -> None:
    """A daily sweep of the local asset cache, plus one at boot.

    Disk is the one resource that fails everything at once: once it is full the
    next run cannot write its media, and neither can anything else. A daily job
    is cheap insurance, and the prune is a no-op unless blob storage is holding
    the durable copy.
    """
    from .orchestrator import prune_asset_cache

    def _prune():
        try:
            prune_asset_cache()
        except Exception as exc:  # noqa: BLE001 - never let tidying stop the scheduler
            logger.warning("Asset cache prune failed: %s", exc)

    # Registering the job must not be able to stop the service booting either:
    # posting is the point, tidying the disk is maintenance.
    try:
        sched.add_job(_prune, CronTrigger.from_crontab("30 4 * * *"), id="housekeeping:assets",
                      replace_existing=True, misfire_grace_time=3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not schedule the asset prune: %s", exc)
    threading.Thread(target=_prune, name="prune-assets-boot", daemon=True).start()

    _schedule_metrics_refresh(sched)


def _schedule_metrics_refresh(sched) -> None:
    """A daily poll of recent posts' performance counters — the feedback loop.

    Unlike the asset prune there is deliberately NO boot sweep: a metrics poll is
    an API call per post — pay-per-use on X — and running it on every gunicorn
    restart would spend credits on each deploy. Once a day is enough for counts
    that feed back into the next scheduled run; the CLI offers an on-demand sweep.
    ``refresh_metrics`` is itself a no-op when ``METRICS_REFRESH_DAYS=0``.
    """
    from .orchestrator import refresh_metrics

    def _refresh():
        try:
            refresh_metrics()
        except Exception as exc:  # noqa: BLE001 - never let tidying stop the scheduler
            logger.warning("Metrics refresh failed: %s", exc)

    try:
        sched.add_job(_refresh, CronTrigger.from_crontab("0 5 * * *"), id="housekeeping:metrics",
                      replace_existing=True, misfire_grace_time=3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not schedule the metrics refresh: %s", exc)


def _reap_stale_runs() -> None:
    """Close out runs abandoned by a previous process. Never fatal.

    Booting is exactly when they exist: a run is only moved off ``running`` by
    the code executing it, so a restart mid-run strands the row. Doing this here
    means a deploy tidies up after itself instead of leaving a Runs page full of
    work that will never finish.
    """
    try:
        from .orchestrator import reap_stale_runs

        reaped = reap_stale_runs()
        if reaped:
            logger.warning("Marked %d abandoned run(s) as failed at startup", len(reaped))
    except Exception as exc:  # noqa: BLE001 - never block the scheduler on tidying
        logger.warning("Could not reap stale runs: %s", exc)
