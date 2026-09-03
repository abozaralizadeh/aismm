"""Plain-language time deltas for the dashboard.

A UTC timestamp answers *when*. An operator reading a "Next run" column is
asking *how soon*, and turning "2026-09-03 14:05 UTC" into "in 3 minutes" in
their head — in a timezone that is probably not UTC — is exactly the arithmetic
a dashboard should have already done. So the absolute time stays (it is the
thing you cross-check against a log) and the relative one is added beside it.

Rendered SERVER-side, like every other number on these pages: it is testable,
it is identical without scripting, and a dashboard page is freshly loaded each
time, so the phrase cannot drift far from the truth. The wording is deliberately
coarse — "in 2 hours", not "in 1 hour 58 minutes 12 seconds" — because the
question is whether to wait or to hit Run now.
"""
from __future__ import annotations

import datetime as dt

__all__ = ["as_utc", "time_until"]


def as_utc(value):
    """A tz-aware UTC datetime, or ``None``.

    SQLite hands back naive datetimes and Azure Table hands back ISO strings, so
    anything reaching a template may be either. A naive value is assumed UTC —
    everything this app stores is.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, dt.datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _phrase(seconds: int) -> str:
    """A rounded duration: 'a moment', '3 minutes', '2h 10m', '4 days'."""
    if seconds < 45:
        return "a moment"
    if seconds < 90:
        return "a minute"

    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} minutes"

    hours, rest = divmod(seconds, 3600)
    minutes = round(rest / 60)
    if minutes == 60:                      # 1h 59m30s must not read "1h 60m"
        hours, minutes = hours + 1, 0
    if hours < 24:
        if minutes:
            return f"{hours}h {minutes}m"
        return "an hour" if hours == 1 else f"{hours} hours"

    days = round(seconds / 86400)
    if days < 14:
        return "a day" if days == 1 else f"{days} days"
    weeks = round(days / 7)
    if weeks < 9:
        return f"{weeks} weeks"
    months = round(days / 30)
    return "a month" if months == 1 else f"{months} months"


def time_until(when, *, now=None) -> str:
    """``'in 3 minutes'`` / ``'2 hours ago'`` / ``'now'``; ``''`` for no time.

    Never raises: a value the template could not turn into a datetime simply
    contributes nothing, because a broken relative line is worse than none
    beside a timestamp that is already correct.
    """
    moment = as_utc(when)
    if moment is None:
        return ""
    reference = as_utc(now) or dt.datetime.now(dt.timezone.utc)
    delta = int((moment - reference).total_seconds())
    if abs(delta) < 45:
        return "now"
    return f"in {_phrase(delta)}" if delta > 0 else f"{_phrase(-delta)} ago"
