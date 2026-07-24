from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from aismm.scheduler import parse_trigger


def test_cron():
    t = parse_trigger("0 9 * * *")
    assert isinstance(t, CronTrigger)


def test_interval_short():
    t = parse_trigger("every 6h")
    assert isinstance(t, IntervalTrigger)
    assert t.interval.total_seconds() == 6 * 3600


def test_interval_bare():
    assert parse_trigger("30m").interval.total_seconds() == 30 * 60
    assert parse_trigger("1d").interval.total_seconds() == 86400


def test_interval_words():
    assert parse_trigger("every 2 hours").interval.total_seconds() == 2 * 3600


def test_interval_floor_60s():
    # sub-minute intervals are floored to 60s to avoid runaway scheduling
    assert parse_trigger("10s").interval.total_seconds() == 60


def test_invalid():
    assert parse_trigger("") is None
    assert parse_trigger("nonsense") is None
