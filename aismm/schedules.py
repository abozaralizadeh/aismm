"""Parsing an instruction's schedule into APScheduler triggers.

``Instruction.schedule`` holds one of two things:

* **A cronai schedule** (JSON, what the instruction page writes now). The page
  embeds the hosted ``<cron-ai>`` widget (https://abozaralizadeh.github.io/cronai/),
  which turns plain English ("every other friday at 5pm") into standard cron
  lines in the operator's time zone and saves ``{"v": 1, "text", "crons",
  "timezone", "description", "everyWeeks"?, "anchor"?}``. :class:`SavedSchedule`
  reads it and :class:`CronLinesTrigger` fires it, reading the cron lines exactly
  as the widget does (see that class), so the runs the page promises are the
  runs that happen. ``tests/fixtures/cronai_runs.json``, generated from the
  hosted engine by ``scripts/make_cronai_fixtures.mjs``, holds us to that.
* **The older text grammar** below, which every instruction saved before the
  widget still carries. It keeps working unchanged (UTC), and the page only
  replaces it when the operator types a different schedule.

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

Everything in this grammar is UTC, so "09:00" means 09:00 UTC. Raw cron is read
as STANDARD cron (0 = Sunday) by :class:`CronLinesTrigger`. It used to go
straight to APScheduler, which numbers weekdays from Monday, so ``0 16 * * 4``
(Thursday) fired on Fridays.
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

import calendar
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger("aismm.schedules")


# --------------------------------------------------------------------------- #
# Cron lines, read the way the cronai widget reads them
# --------------------------------------------------------------------------- #
# APScheduler's CronTrigger is NOT standard cron, in two ways that matter here:
# weekday 0 is MONDAY (cron: Sunday), and a line restricting both the day of the
# month and the weekday fires when BOTH match (cron: when EITHER does). It also
# has no `5L` / `1#2`. The widget shows the operator the next runs it computed
# with standard rules, so we evaluate the lines ourselves with the same rules
# rather than translate them onto APScheduler's and hope they line up.

_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                "dec")
_WEEKDAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")   # cron: 0 = Sunday
_CRON_MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
                "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
                "@hourly": "0 * * * *"}
_FIELD_BOUNDS = {"minute": (0, 59), "hour": (0, 23), "day": (1, 31), "month": (1, 12),
                 "weekday": (0, 6)}
_WEEK_SECONDS = 604800
# A valid line always fires within 8 years (29 February on a given weekday).
_SEARCH_DAYS = 366 * 8


class ScheduleError(ValueError):
    """A schedule that cannot be run, with a reason fit to show the operator."""


def _cron_value(token: str, field: str) -> int:
    low = token.lower()
    if low.isalpha():
        names = {"month": _MONTH_NAMES, "weekday": _WEEKDAY_NAMES}.get(field, ())
        if low[:3] in names:
            return names.index(low[:3]) + (1 if field == "month" else 0)
        raise ScheduleError(f"{token!r} is not a valid {field}")
    if not token.isdigit():
        raise ScheduleError(f"{token!r} is not a valid {field}")
    value = int(token)
    low_bound, high_bound = _FIELD_BOUNDS[field]
    # 7 is Sunday as well as 0, so a weekday may say 7 (folded to 0 later).
    if not low_bound <= value <= (7 if field == "weekday" else high_bound):
        raise ScheduleError(f"{field} {value} is out of range ({low_bound}-{high_bound})")
    return value


def _expand(part: str, field: str) -> list[int]:
    """One comma-separated piece of a field: ``*``, ``5``, ``1-5``, ``*/15``, ``fri-mon``."""
    low_bound, high_bound = _FIELD_BOUNDS[field]
    span, slash, step_text = part.partition("/")
    if slash and (not step_text.isdigit() or int(step_text) < 1):
        raise ScheduleError(f"invalid step '/{step_text}' in the {field}")
    step = int(step_text) if slash else 1
    if span in ("*", "?"):
        first, last = low_bound, high_bound
    elif "-" in span:
        start, _, end = span.partition("-")
        first, last = _cron_value(start, field), _cron_value(end, field)
        if last < first:                        # wraps around: fri-mon, 22-2
            values = list(range(first, high_bound + 1, step))
            return values + list(range(low_bound, last + 1, step))
    else:
        first = _cron_value(span, field)
        last = high_bound if slash else first
    # `1-7` keeps 7 here and becomes Sunday below: Monday through Sunday.
    return list(range(first, last + 1, step))


class CronLine:
    """One standard 5-field cron line: ``minute hour day month weekday``.

    Besides plain cron it takes what the widget can emit: names (JAN, MON),
    ``?``, the ``@daily``-style macros, ``L`` (last day of the month) in the
    day field, and ``5L`` (last Friday) / ``1#2`` (second Monday) in the weekday
    field. When BOTH day fields are restricted, the line fires when EITHER
    matches, as every cron daemon does.
    """

    def __init__(self, expr: str):
        text = (expr or "").strip()
        fields = _CRON_MACROS.get(text.lower(), text).split()
        if len(fields) != 5:
            raise ScheduleError(f"{expr!r}: a cron line needs exactly 5 fields "
                                "(minute hour day month weekday)")
        minute, hour, day, month, weekday = fields
        self.expr = text
        self.fields = fields
        self.minutes = sorted(self._values(minute, "minute"))
        self.hours = sorted(self._values(hour, "hour"))
        self.months = self._values(month, "month")

        self.day_any = day in ("*", "?")
        self.days: set[int] = set()
        self.last_day = False
        if not self.day_any:
            for part in self._parts(day, "day"):
                if part.upper() == "L":
                    self.last_day = True
                else:
                    self.days.update(_expand(part, "day"))

        self.weekday_any = weekday in ("*", "?")
        self.weekdays: set[int] = set()
        self.nth: set[tuple[int, int]] = set()      # (weekday, k): k-th one of the month
        self.last_weekdays: set[int] = set()       # last such weekday of the month
        if not self.weekday_any:
            for part in self._parts(weekday, "weekday"):
                nth = re.fullmatch(r"(\w+)#([1-5])", part)
                last = re.fullmatch(r"(\w+)L", part, re.IGNORECASE)
                if nth:
                    self.nth.add((_cron_value(nth.group(1), "weekday") % 7, int(nth.group(2))))
                elif last:
                    self.last_weekdays.add(_cron_value(last.group(1), "weekday") % 7)
                else:
                    self.weekdays.update(v % 7 for v in _expand(part, "weekday"))

    @staticmethod
    def _parts(text: str, field: str) -> list[str]:
        parts = text.split(",")
        if any(not p for p in parts):
            raise ScheduleError(f"empty value in the {field}")
        return parts

    def _values(self, text: str, field: str) -> set[int]:
        return {v for part in self._parts(text, field) for v in _expand(part, field)}

    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        days_in_month = calendar.monthrange(day.year, day.month)[1]
        weekday = (day.weekday() + 1) % 7                    # Python: 0 = Monday
        day_hit = day.day in self.days or (self.last_day and day.day == days_in_month)
        weekday_hit = (weekday in self.weekdays
                       or (weekday, (day.day + 6) // 7) in self.nth
                       or (weekday in self.last_weekdays and day.day + 7 > days_in_month))
        if self.day_any and self.weekday_any:
            return True
        if self.day_any:
            return weekday_hit
        if self.weekday_any:
            return day_hit
        return day_hit or weekday_hit


def _from_wall(day: date, hour: int, minute: int, zone) -> datetime | None:
    """The instant a wall-clock time happens in ``zone``; None if DST skips it.

    A time that happens twice (the clocks going back) is its FIRST occurrence,
    which is ``fold=0``. Both choices are cronai's.
    """
    naive = datetime(day.year, day.month, day.day, hour, minute)
    instant = naive.replace(tzinfo=zone).astimezone(dt_timezone.utc)
    if instant.astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return instant


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown time zone {name!r}") from exc


class CronLinesTrigger(BaseTrigger):
    """Fires at every moment any of several cron lines names, in one time zone.

    ONE trigger for all the lines, so the instruction gets one job: a moment two
    lines share fires once ("every 90 minutes" is two lines). ``every_weeks``
    with ``week_anchor`` is cronai's week guard: cron cannot skip weeks, so
    "every other friday" is a weekly line that only counts in weeks where
    ``floor((t - anchor) / 1 week)`` is a multiple of ``every_weeks``.
    ``start_date`` is "don't fire before", as on the APScheduler triggers.
    """

    def __init__(self, crons, timezone: str = "UTC", *, every_weeks: int = 1,
                 week_anchor: int | None = None, start_date: datetime | None = None):
        self.crons = tuple(c.strip() for c in crons)
        if not self.crons:
            raise ScheduleError("no cron lines")
        self.lines = [CronLine(c) for c in self.crons]
        self.timezone_name = timezone
        self.timezone = _zone(timezone)
        self.every_weeks = max(1, int(every_weeks or 1))
        self.week_anchor = week_anchor
        if start_date is not None and start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=dt_timezone.utc)
        self.start_date = start_date

    def get_next_fire_time(self, previous_fire_time, now):
        # Same bookkeeping as APScheduler's CronTrigger, so misfire handling and
        # coalescing behave as they always have.
        if previous_fire_time:
            start = min(now, previous_fire_time + timedelta(microseconds=1))
            if start == previous_fire_time:
                start += timedelta(microseconds=1)
        else:
            start = max(now, self.start_date) if self.start_date else now
        return self.first_at_or_after(start)

    def first_at_or_after(self, start: datetime) -> datetime | None:
        first_day = start.astimezone(self.timezone).date()
        for offset in range(_SEARCH_DAYS + 7 * self.every_weeks):
            day = first_day + timedelta(days=offset)
            best = None
            for line in self.lines:
                if line.matches_day(day):
                    moment = self._first_on(line, day, start)
                    if moment and (best is None or moment < best):
                        best = moment
            if best:
                return best.astimezone(self.timezone)
        return None

    def _first_on(self, line: CronLine, day: date, start: datetime) -> datetime | None:
        for hour in line.hours:
            for minute in line.minutes:
                moment = _from_wall(day, hour, minute, self.timezone)
                if moment is not None and moment >= start and self._week_allows(moment):
                    return moment
        return None

    def _week_allows(self, moment: datetime) -> bool:
        if self.every_weeks < 2 or self.week_anchor is None:
            return True
        week = int((moment.timestamp() - self.week_anchor) // _WEEK_SECONDS)
        return week % self.every_weeks == 0

    def __str__(self) -> str:
        weeks = f", every {self.every_weeks} weeks" if self.every_weeks > 1 else ""
        return f"cron[{'; '.join(self.crons)}] {self.timezone_name}{weeks}"

    def __repr__(self) -> str:
        return f"<CronLinesTrigger ({self})>"


@dataclass(frozen=True)
class SavedSchedule:
    """What the cronai widget saves: the words typed and the cron lines they mean."""

    text: str
    crons: tuple[str, ...]
    timezone: str
    description: str = ""
    every_weeks: int = 1
    anchor: int | None = None

    def trigger(self, *, start_date: datetime | None = None) -> CronLinesTrigger:
        return CronLinesTrigger(self.crons, self.timezone, every_weeks=self.every_weeks,
                                week_anchor=self.anchor, start_date=start_date)

    def to_json(self) -> str:
        data = {"v": 1, "text": self.text, "crons": list(self.crons),
                "timezone": self.timezone, "description": self.description}
        if self.every_weeks > 1:
            data.update(everyWeeks=self.every_weeks, anchor=self.anchor)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


_MAX_TEXT = 300
_MAX_LINES = 24


def is_saved(schedule: str | None) -> bool:
    """Is this a widget schedule (JSON) rather than the older text grammar?"""
    return (schedule or "").lstrip().startswith("{")


def read_saved(schedule: str) -> SavedSchedule:
    """Validate a widget schedule. Raises :class:`ScheduleError` saying why not.

    Everything the scheduler will act on is checked here, server-side: the JSON
    comes from a browser, so a value that would never fire, or fire somewhere
    unexpected, is refused rather than stored.
    """
    try:
        data = json.loads(schedule)
    except (TypeError, ValueError) as exc:
        raise ScheduleError("the saved schedule is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ScheduleError("not a cronai schedule (expected version 1)")
    text = data.get("text")
    crons = data.get("crons")
    timezone = data.get("timezone") or "UTC"
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT:
        raise ScheduleError("the schedule has no readable text")
    if (not isinstance(crons, list) or not crons or len(crons) > _MAX_LINES
            or not all(isinstance(c, str) for c in crons)):
        raise ScheduleError("the schedule has no cron lines")
    if not isinstance(timezone, str):
        raise ScheduleError("the time zone is not a name")
    description = data.get("description") or ""
    every_weeks = data.get("everyWeeks") or 1
    anchor = data.get("anchor")
    if isinstance(every_weeks, bool) or not isinstance(every_weeks, int) \
            or not 1 <= every_weeks <= 52:
        raise ScheduleError("every-N-weeks must be between 1 and 52")
    if every_weeks > 1 and (isinstance(anchor, bool) or not isinstance(anchor, int)):
        raise ScheduleError("an every-N-weeks schedule needs its week anchor")
    saved = SavedSchedule(text=text.strip(), crons=tuple(c.strip() for c in crons),
                          timezone=timezone, description=str(description)[:500],
                          every_weeks=every_weeks, anchor=anchor if every_weeks > 1 else None)
    saved.trigger()                 # every line parses, the zone exists
    return saved


def label(schedule: str | None) -> str:
    """The schedule as the operator wrote it: the widget's words, or the old text."""
    if is_saved(schedule):
        try:
            return read_saved(schedule).text
        except ScheduleError:
            return "(unreadable schedule)"
    return (schedule or "").strip()


def from_widget(posted: str, *, typed: str | None, shown: str, current: str) -> str:
    """What to store after the instruction page is saved with the widget on it.

    ``posted`` is the widget's form value: its schedule JSON, or ``""`` when the
    box is empty OR not understood (the widget cannot tell us which). ``typed``
    is the words in the box, mirrored by the page's script, and ``shown`` is the
    schedule the page was rendered with.

    What the box shows is what is saved, with one exception: a schedule in the
    OLDER grammar whose words and time zone (UTC) were left alone is kept exactly
    as it is. The widget re-reads those words on load, but it reads them as
    clock-aligned cron, so storing its reading would quietly move an "every 90m"
    off its phase whenever someone saved the brief; and if the widget cannot
    read them at all it posts "", which must not wipe the schedule.
    Raises :class:`ScheduleError` for words that were changed but not understood.
    """
    posted = (posted or "").strip()
    saved = read_saved(posted) if posted else None
    if typed is None:                     # the page's script did not run
        if saved is None:
            return current                # "emptied" and "not understood" look alike
        typed = saved.text
    typed = typed.strip()
    untouched = bool(shown.strip()) and typed == label(shown)
    if untouched and saved is None:
        return current
    if untouched and not is_saved(shown) and saved.timezone == "UTC":
        return current
    if not typed:
        return ""
    if saved is None:
        raise ScheduleError(f"“{typed}” was not understood as a schedule")
    return saved.to_json()


def from_text(text: str, *, current: str) -> str:
    """What to store from the plain text box (no widget: a script, or the CDN was down).

    The old grammar is stored as typed. Pasted widget JSON is validated first,
    since a broken one would otherwise be saved and silently never fire.
    """
    text = (text or "").strip()
    if text == (current or "").strip() or not is_saved(text):
        return text
    return read_saved(text).to_json()


def fires_per_day(schedule: str | None, *, now: datetime | None = None, days: int = 7) -> float:
    """How often a schedule fires, averaged over the next ``days`` days.

    Format-independent, so "you post more often than an account can" advice
    works for widget schedules ("every hour") as well as the older "every 1h".
    """
    now = now or datetime.now(dt_timezone.utc)
    end = now + timedelta(days=days)
    count = 0
    # Anchored at `now`: an un-anchored interval starts one interval LATER, which
    # would count "every 2h" as 83 fires a week instead of 84.
    for trigger in parse_schedule(schedule or "", anchor=now):
        fire = trigger.get_next_fire_time(None, now)
        while fire and fire < end and count < 5000:
            count += 1
            fire = trigger.get_next_fire_time(fire, fire)
    return count / days

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


def _cron_from_crontab(expr: str, anchor: datetime | None) -> CronLinesTrigger:
    """A raw cron line in the old grammar: standard cron, UTC, gated by ``anchor``.

    Not ``CronTrigger(...)``: APScheduler reads weekday 4 as Friday.
    """
    return CronLinesTrigger([expr], "UTC", start_date=anchor)


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
    if is_saved(schedule):
        try:
            return [read_saved(schedule).trigger(start_date=anchor)]
        except ScheduleError as exc:
            logger.warning("Unusable saved schedule %r: %s", schedule, exc)
            return []
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


_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}
_MONDAY_FIRST = (1, 2, 3, 4, 5, 6, 0)


def _weekday_text(line: CronLine) -> str:
    """A line's weekdays, Monday first: "mon-fri", "tue,thu,sun", "2nd mon", "last fri"."""
    chosen = [d for d in _MONDAY_FIRST if d in line.weekdays]
    names, run = [], []
    for weekday in chosen + [None]:
        if weekday is not None and run and _MONDAY_FIRST.index(weekday) == \
                _MONDAY_FIRST.index(run[-1]) + 1:
            run.append(weekday)
            continue
        if len(run) >= 3:
            names.append(f"{_WEEKDAY_NAMES[run[0]]}-{_WEEKDAY_NAMES[run[-1]]}")
        else:
            names.extend(_WEEKDAY_NAMES[d] for d in run)
        run = [weekday] if weekday is not None else []
    for weekday, k in sorted(line.nth, key=lambda p: (p[1], _MONDAY_FIRST.index(p[0]))):
        names.append(f"{_ORDINALS[k]} {_WEEKDAY_NAMES[weekday]}")
    names.extend(f"last {_WEEKDAY_NAMES[d]}" for d in _MONDAY_FIRST if d in line.last_weekdays)
    return ",".join(names)


def _describe_line(line: CronLine) -> str:
    """Readback of one raw cron line of the OLD grammar (always UTC)."""
    minute, hour, day, month, _ = line.fields
    if minute.isdigit() and re.fullmatch(r"\d+(,\d+)*", hour):
        when = "at " + " and ".join(f"{h:02d}:{int(minute):02d}" for h in line.hours)
    elif minute.isdigit() and hour in ("*", "?"):
        when = f"every hour at minute {minute}"
    else:
        when = f"cron hour={hour} minute={minute}"
    piece = f"{when} UTC"
    days = []
    if not line.day_any:
        days.append("the last day of the month" if day.upper() == "L"
                    else "day " + day.replace("L", "last"))
    if not line.weekday_any:
        days.append(_weekday_text(line))
    if days:
        piece += " on " + " or ".join(days)
    if month not in ("*", "?"):
        piece += " in " + ",".join(_MONTH_NAMES[m - 1] for m in sorted(line.months))
    # The same cross-product warning as the time-of-day form below.
    if (line.day_any and month in ("*", "?") and not line.nth and not line.last_weekdays
            and len(line.hours) > 1 and when.startswith("at ")):
        fires = len(line.hours) * (7 if line.weekday_any else len(line.weekdays))
        if fires > 1:
            piece += f" — {fires}× a week"
    return piece


def describe(schedule: str, *, starts_at: datetime | None = None) -> str:
    """Plain-English readback of what a schedule string was understood to mean.

    ``starts_at`` is the operator-set field, not the ``created_at`` fallback used
    for the actual anchor — only an EXPLICIT start is worth telling them about.
    A widget schedule reads back as the widget described it, plus its time zone.
    """
    if is_saved(schedule):
        try:
            saved = read_saved(schedule)
        except ScheduleError as exc:
            return f"not understood ({exc}) — this instruction will never fire"
        rendered = f"{saved.description or '; '.join(saved.crons)} ({saved.timezone})"
        if starts_at:
            rendered += f", starting {starts_at.strftime('%Y-%m-%d %H:%M')} UTC"
        return rendered
    triggers = parse_schedule(schedule, anchor=starts_at)
    if not triggers:
        return "not understood — this instruction will never fire"
    pieces = []
    for trigger in triggers:
        if isinstance(trigger, CronLinesTrigger):
            pieces.extend(_describe_line(line) for line in trigger.lines)
        elif isinstance(trigger, IntervalTrigger):
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
