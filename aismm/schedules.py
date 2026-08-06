"""Parsing an instruction's schedule into APScheduler triggers.

One instruction can fire on several triggers, because that is how people
actually describe a posting cadence: *"09:00 and 18:00 on weekdays"* is two
times, and *"every 6h, plus 08:00 Monday"* mixes an interval with a fixed time.
``parse_schedule`` therefore returns a **list** of triggers, and the scheduler
registers one job per trigger.

Accepted forms, combined freely with ``,`` / ``;`` / ``and`` / newlines:

    09:00                      every day at 09:00 UTC
    9am, 6pm                   twice a day
    09:00 mon-fri              weekdays only
    09:00,18:00 mon,wed,fri    several times, several days
    every 6h                   interval (also 30m / 90s / 2 days / 6h)
    hourly · daily · weekly    named intervals
    0 9 * * *                  raw 5-field cron, still supported
    @daily                     cron nicknames

Everything is UTC — the scheduler runs on it, so "09:00" means 09:00 UTC.
:func:`describe` renders a parsed schedule back as English for the dashboard, so
the operator can see what their text was understood to mean.

An ``every Xh``-style schedule needs a fixed reference point ("every 6 hours
starting FROM WHEN?"), and every trigger built here takes an ``anchor`` for that.
Without one, ``IntervalTrigger`` anchors to the moment it was *constructed* — so
re-registering the same "every 1h" job (which happens on every dashboard save of
ANY instruction, and on every service restart, since :func:`aismm.scheduler.
refresh_jobs` rebuilds every job from scratch) silently pushed the next fire a
full interval into the future each time. Callers pass a stable anchor —
``instruction.schedule_start_at`` if the operator set one, else
``instruction.created_at`` — so the phase survives being rebuilt. Cron-style
parts ("09:00", raw cron) don't drift this way; ``anchor`` only gates them
("don't fire before this"), which is a no-op once the anchor is in the past.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger("aismm.schedules")

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_WORD_UNIT = {"second": "s", "sec": "s", "minute": "m", "min": "m", "hour": "h",
              "hr": "h", "day": "d", "week": "w"}
_NAMED = {
    "hourly": ("interval", 3600), "daily": ("cron", "0 0 * * *"),
    "nightly": ("cron", "0 0 * * *"), "weekly": ("cron", "0 0 * * 0"),
    "monthly": ("cron", "0 0 1 * *"), "midnight": ("cron", "0 0 * * *"),
    "noon": ("cron", "0 12 * * *"),
}
_DAYS = {"mon": "mon", "monday": "mon", "tue": "tue", "tues": "tue", "tuesday": "tue",
         "wed": "wed", "weds": "wed", "wednesday": "wed", "thu": "thu", "thur": "thu",
         "thurs": "thu", "thursday": "thu", "fri": "fri", "friday": "fri",
         "sat": "sat", "saturday": "sat", "sun": "sun", "sunday": "sun"}
_DAY_GROUPS = {"weekday": "mon-fri", "weekdays": "mon-fri", "weekend": "sat,sun",
               "weekends": "sat,sun", "everyday": "*", "daily": "*"}

# "09:00", "9:00", "9am", "09.30", "0900"
_TIME = re.compile(r"^(\d{1,2})(?::|\.)?(\d{2})?\s*(am|pm)?$", re.IGNORECASE)
_INTERVAL = re.compile(
    r"^(?:every\s+)?(\d+)\s*"
    r"([smhdw]|seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)$", re.IGNORECASE)
_MIN_INTERVAL_SECONDS = 60


def _split_parts(schedule: str) -> list[str]:
    """Split a combined schedule into independent parts."""
    text = (schedule or "").strip()
    if not text:
        return []
    # "and" joins times WITHIN one part, so "09:30 and 17:45 weekdays" applies
    # the weekday filter to both. Only ";" and newlines start a new part.
    normalized = re.sub(r"\s+and\s+", ",", text, flags=re.IGNORECASE)
    normalized = normalized.replace("\n", ";")
    # A 5-field cron has spaces but no separator — don't split it.
    return [p.strip() for p in re.split(r"[;]+", normalized) if p.strip()]


def _parse_time(token: str) -> tuple[int, int] | None:
    match = _TIME.match(token.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    # A bare number without ":" or am/pm is ambiguous ("6" = 6am? every 6h?).
    # Require a separator or a meridiem so "6" is not silently 06:00.
    if match.group(2) is None and not meridiem:
        return None
    return hour, minute


def _parse_days(tokens: list[str]) -> str | None:
    """Turn day words into a cron day-of-week field ("mon-fri", "mon,wed")."""
    parts: list[str] = []
    for token in tokens:
        low = token.lower().strip(",")
        if low in _DAY_GROUPS:
            parts.append(_DAY_GROUPS[low])
        elif low in _DAYS:
            parts.append(_DAYS[low])
        elif "-" in low:                      # mon-fri
            ends = [_DAYS.get(p.strip()) for p in low.split("-", 1)]
            if all(ends):
                parts.append(f"{ends[0]}-{ends[1]}")
            else:
                return None
        else:
            return None
    return ",".join(parts) if parts else None


def _cron_from_crontab(expr: str, anchor: datetime | None) -> CronTrigger:
    """``CronTrigger.from_crontab`` doesn't take ``start_date``, so build it directly."""
    minute, hour, day, month, day_of_week = expr.split()
    return CronTrigger(minute=minute, hour=hour, day=day, month=month,
                       day_of_week=day_of_week, start_date=anchor, timezone="UTC")


def _cron_from_times(times: list[tuple[int, int]], days: str | None,
                     anchor: datetime | None):
    """One CronTrigger covering several times of day (cron takes lists)."""
    hours = ",".join(str(h) for h, _ in times)
    minutes = ",".join(sorted({str(m) for _, m in times}))
    if len({m for _, m in times}) > 1:
        # Different minutes per hour can't be one cron field pair without
        # cross-producting, so the caller splits those into separate triggers.
        return None
    return CronTrigger(hour=hours, minute=minutes, day_of_week=days or "*",
                       start_date=anchor, timezone="UTC")


def _parse_part(part: str, anchor: datetime | None = None) -> list:
    """Parse one part into zero or more triggers."""
    text = part.strip()
    low = text.lower()

    if low.startswith("@"):                                    # cron nicknames
        try:
            return [_cron_from_crontab(
                {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
                 "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
                 "@hourly": "0 * * * *"}[low], anchor)]
        except KeyError:
            return []

    if low in _NAMED:
        kind, value = _NAMED[low]
        if kind == "interval":
            return [IntervalTrigger(seconds=value, start_date=anchor, timezone="UTC")]
        return [_cron_from_crontab(value, anchor)]

    interval = _INTERVAL.match(low)
    if interval:
        count, raw_unit = int(interval.group(1)), interval.group(2).lower()
        unit = raw_unit if raw_unit in _UNIT_SECONDS else _WORD_UNIT.get(
            raw_unit.rstrip("s"), "h")
        seconds = max(count * _UNIT_SECONDS.get(unit, 3600), _MIN_INTERVAL_SECONDS)
        return [IntervalTrigger(seconds=seconds, start_date=anchor, timezone="UTC")]

    if len(text.split()) == 5:                                 # raw cron
        try:
            return [_cron_from_crontab(text, anchor)]
        except ValueError as exc:
            logger.warning("Invalid cron %r: %s", text, exc)
            return []

    # "09:00,18:00 mon-fri" — times first, then optional day words.
    tokens = [t for t in re.split(r"[\s]+", text) if t]
    time_tokens: list[str] = []
    day_tokens: list[str] = []
    for token in tokens:
        pieces = [p for p in token.split(",") if p]
        if all(_parse_time(p) is not None for p in pieces):
            time_tokens.extend(pieces)
        else:
            day_tokens.extend(pieces)
    times = [_parse_time(t) for t in time_tokens]
    times = [t for t in times if t]
    if not times:
        return []
    days = _parse_days(day_tokens) if day_tokens else None
    if day_tokens and days is None:
        logger.warning("Unrecognized day names in %r", text)
        return []

    combined = _cron_from_times(times, days, anchor)
    if combined is not None:
        return [combined]
    return [CronTrigger(hour=str(h), minute=str(m), day_of_week=days or "*",
                        start_date=anchor, timezone="UTC") for h, m in times]


def parse_schedule(schedule: str, *, anchor: datetime | None = None) -> list:
    """All triggers for a schedule string. Empty list = nothing valid found.

    ``anchor`` is the interval phase reference / "don't fire before" gate — see
    the module docstring. Pass ``instruction.schedule_start_at or
    instruction.created_at`` from callers that have an ``Instruction``.
    """
    triggers = []
    for part in _split_parts(schedule):
        parsed = _parse_part(part, anchor)
        if not parsed:
            logger.warning("Unrecognized schedule part %r", part)
        triggers.extend(parsed)
    return triggers


def parse_trigger(schedule: str, *, anchor: datetime | None = None):
    """First trigger only — kept for callers that want a single trigger."""
    triggers = parse_schedule(schedule, anchor=anchor)
    return triggers[0] if triggers else None


_ALL_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _count_days(day_of_week: str) -> int:
    """How many weekdays a cron day field covers, or 0 if it cannot be counted."""
    if day_of_week in ("*", "mon-sun"):
        return 7
    chosen = set()
    for part in day_of_week.split(","):
        part = part.strip()
        if part in _ALL_DAYS:
            chosen.add(part)
        elif "-" in part:                      # a range: mon-fri
            start, _, end = part.partition("-")
            if start not in _ALL_DAYS or end not in _ALL_DAYS:
                return 0
            first, last = _ALL_DAYS.index(start), _ALL_DAYS.index(end)
            span = (_ALL_DAYS[first:last + 1] if first <= last
                    else _ALL_DAYS[first:] + _ALL_DAYS[:last + 1])
            chosen.update(span)
        else:
            return 0                            # a step, or something unparsed
    return len(chosen)


def _weekly_fires(hour: str, day_of_week: str) -> int:
    """How many times a week one cron part fires, or 0 when it is not worth saying.

    Only counted for a list of literal hours (steps like ``*/4`` are left alone —
    a wrong count is worse than none), and only reported when there is more than
    ONE time of day. That is when times multiply across days, which is the thing
    worth seeing: a schedule with a single daily time does not need to be told it
    runs seven times a week.
    """
    if not hour.replace(",", "").isdigit():
        return 0
    times = len({h for h in hour.split(",")})
    if times < 2:
        return 0
    days = _count_days(day_of_week)
    return times * days if days else 0


def describe(schedule: str, *, starts_at: datetime | None = None) -> str:
    """Plain-English readback of what a schedule string was understood to mean.

    ``starts_at`` is the operator-set field, not the ``created_at`` fallback used
    for the actual anchor — only an EXPLICIT start is worth telling them about.
    """
    triggers = parse_schedule(schedule, anchor=starts_at)
    if not triggers:
        return "not understood — this instruction will never fire"
    pieces = []
    for trigger in triggers:
        if isinstance(trigger, IntervalTrigger):
            total = int(trigger.interval.total_seconds())
            for unit, seconds in (("week", 604800), ("day", 86400), ("hour", 3600),
                                  ("minute", 60)):
                if total >= seconds and total % seconds == 0:
                    count = total // seconds
                    pieces.append(f"every {count} {unit}{'s' if count > 1 else ''}")
                    break
            else:
                pieces.append(f"every {total}s")
        else:
            fields = {f.name: str(f) for f in trigger.fields}
            hour, minute = fields.get("hour", "*"), fields.get("minute", "*")
            day_of_week = fields.get("day_of_week", "*")
            if hour == "*":
                when = f"every hour at minute {minute}"
            elif hour.replace(",", "").isdigit() and minute.isdigit():
                # Cron holds several hours as "9,18"; render each as HH:MM. Deduped
                # and sorted: "03:00 thu, 03:00 tue" collapses both times into ONE
                # cron field, and reading back "at 03:00 and 03:00" looks like a
                # bug rather than like the cross-product it actually is.
                hours = sorted({int(h) for h in hour.split(",")})
                when = "at " + " and ".join(f"{h:02d}:{int(minute):02d}" for h in hours)
            else:
                # Steps and ranges ("*/4", "9-17") — show the cron fields as-is.
                when = f"cron hour={hour} minute={minute}"
            days = "" if day_of_week in ("*", "mon-sun") else f" on {day_of_week}"
            piece = f"{when} UTC{days}"
            # Several times AND several days in one part is a CROSS-PRODUCT: it
            # fires at every listed time on every listed day. That is what
            # separating with commas means, and it is not what someone writing
            # "03:00 thu, 03:00 tue, 15:00 sun" usually wants — so say the number
            # out loud, because 6 vs 3 is the whole difference.
            fires = _weekly_fires(hour, day_of_week)
            if fires and fires > 1:
                piece += f" — {fires}× a week"
            pieces.append(piece)
    rendered = " · ".join(pieces)
    if starts_at:
        rendered += f", starting {starts_at.strftime('%Y-%m-%d %H:%M')} UTC"
    return rendered
