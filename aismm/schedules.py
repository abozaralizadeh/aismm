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
"""
from __future__ import annotations

import logging
import re

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


def _cron_from_times(times: list[tuple[int, int]], days: str | None):
    """One CronTrigger covering several times of day (cron takes lists)."""
    hours = ",".join(str(h) for h, _ in times)
    minutes = ",".join(sorted({str(m) for _, m in times}))
    if len({m for _, m in times}) > 1:
        # Different minutes per hour can't be one cron field pair without
        # cross-producting, so the caller splits those into separate triggers.
        return None
    return CronTrigger(hour=hours, minute=minutes, day_of_week=days or "*",
                       timezone="UTC")


def _parse_part(part: str) -> list:
    """Parse one part into zero or more triggers."""
    text = part.strip()
    low = text.lower()

    if low.startswith("@"):                                    # cron nicknames
        try:
            return [CronTrigger.from_crontab(
                {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
                 "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
                 "@hourly": "0 * * * *"}[low], timezone="UTC")]
        except KeyError:
            return []

    if low in _NAMED:
        kind, value = _NAMED[low]
        if kind == "interval":
            return [IntervalTrigger(seconds=value, timezone="UTC")]
        return [CronTrigger.from_crontab(value, timezone="UTC")]

    interval = _INTERVAL.match(low)
    if interval:
        count, raw_unit = int(interval.group(1)), interval.group(2).lower()
        unit = raw_unit if raw_unit in _UNIT_SECONDS else _WORD_UNIT.get(
            raw_unit.rstrip("s"), "h")
        seconds = max(count * _UNIT_SECONDS.get(unit, 3600), _MIN_INTERVAL_SECONDS)
        return [IntervalTrigger(seconds=seconds, timezone="UTC")]

    if len(text.split()) == 5:                                 # raw cron
        try:
            return [CronTrigger.from_crontab(text, timezone="UTC")]
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

    combined = _cron_from_times(times, days)
    if combined is not None:
        return [combined]
    return [CronTrigger(hour=str(h), minute=str(m), day_of_week=days or "*",
                        timezone="UTC") for h, m in times]


def parse_schedule(schedule: str) -> list:
    """All triggers for a schedule string. Empty list = nothing valid found."""
    triggers = []
    for part in _split_parts(schedule):
        parsed = _parse_part(part)
        if not parsed:
            logger.warning("Unrecognized schedule part %r", part)
        triggers.extend(parsed)
    return triggers


def parse_trigger(schedule: str):
    """First trigger only — kept for callers that want a single trigger."""
    triggers = parse_schedule(schedule)
    return triggers[0] if triggers else None


def describe(schedule: str) -> str:
    """Plain-English readback of what a schedule string was understood to mean."""
    triggers = parse_schedule(schedule)
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
                # Cron holds several hours as "9,18"; render each as HH:MM.
                when = "at " + " and ".join(
                    f"{int(h):02d}:{int(minute):02d}" for h in hour.split(","))
            else:
                # Steps and ranges ("*/4", "9-17") — show the cron fields as-is.
                when = f"cron hour={hour} minute={minute}"
            days = "" if day_of_week in ("*", "mon-sun") else f" on {day_of_week}"
            pieces.append(f"{when} UTC{days}")
    return " · ".join(pieces)
