"""Schedule parsing: times of day, weekday filters, intervals, cron, combined.

A posting cadence is usually several triggers ("09:00 and 18:00 on weekdays"),
so ``parse_schedule`` returns a list and the scheduler registers one job each.
"""
import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from aismm.schedules import describe, parse_schedule, parse_trigger


def _fields(trigger):
    return {f.name: str(f) for f in trigger.fields}


# --- times of day ------------------------------------------------------------------ #

@pytest.mark.parametrize("text,hour,minute", [
    ("09:00", "9", "0"),
    ("9:30", "9", "30"),
    ("09.45", "9", "45"),
    ("9am", "9", "0"),
    ("6pm", "18", "0"),
    ("12am", "0", "0"),
    ("12pm", "12", "0"),
    ("23:59", "23", "59"),
])
def test_single_time(text, hour, minute):
    triggers = parse_schedule(text)
    assert len(triggers) == 1
    assert isinstance(triggers[0], CronTrigger)
    fields = _fields(triggers[0])
    assert (fields["hour"], fields["minute"]) == (hour, minute)


def test_several_times_share_one_trigger_when_minutes_match():
    """Cron can express "09:00 and 18:00" as one trigger — no need for two jobs."""
    triggers = parse_schedule("9am, 6pm")
    assert len(triggers) == 1
    assert _fields(triggers[0])["hour"] == "9,18"


def test_times_with_different_minutes_become_separate_triggers():
    """A single cron can't say 09:30 + 17:45 without cross-producting them."""
    triggers = parse_schedule("09:30, 17:45")
    assert len(triggers) == 2
    assert {(_fields(t)["hour"], _fields(t)["minute"]) for t in triggers} == {
        ("9", "30"), ("17", "45")}


@pytest.mark.parametrize("text", ["6", "25:00", "09:99", "garbage", "", "   "])
def test_unparseable_yields_no_triggers(text):
    """Better nothing than a wrong guess — a bare "6" could be 6am or every 6h."""
    assert parse_schedule(text) == []


# --- weekday filters ---------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("09:00 mon-fri", "mon-fri"),
    ("09:00 weekdays", "mon-fri"),
    ("09:00 weekends", "sat,sun"),
    ("09:00 mon,wed,fri", "mon,wed,fri"),
    ("09:00 monday", "mon"),
    ("09:00 sat", "sat"),
])
def test_day_filters(text, expected):
    triggers = parse_schedule(text)
    assert len(triggers) == 1
    assert _fields(triggers[0])["day_of_week"] == expected


def test_unknown_day_words_are_rejected_not_ignored(caplog):
    """Silently dropping "on tuesdayish" would post every day unexpectedly."""
    assert parse_schedule("09:00 someday") == []


def test_and_keeps_the_day_filter_for_every_time():
    """"09:30 and 17:45 weekdays" means BOTH on weekdays."""
    triggers = parse_schedule("09:30 and 17:45 weekdays")
    assert len(triggers) == 2
    assert all(_fields(t)["day_of_week"] == "mon-fri" for t in triggers)


def test_three_times_on_weekends():
    triggers = parse_schedule("9am and 1pm and 6pm weekends")
    assert len(triggers) == 1
    assert _fields(triggers[0])["hour"] == "9,13,18"
    assert _fields(triggers[0])["day_of_week"] == "sat,sun"


# --- intervals ---------------------------------------------------------------------- #

@pytest.mark.parametrize("text,seconds", [
    ("every 6h", 6 * 3600),
    ("6h", 6 * 3600),
    ("30m", 1800),
    ("every 2 hours", 2 * 3600),
    ("every 90 minutes", 5400),
    ("1d", 86400),
    ("every 2 weeks", 2 * 604800),
    ("hourly", 3600),
])
def test_intervals(text, seconds):
    triggers = parse_schedule(text)
    assert len(triggers) == 1
    assert isinstance(triggers[0], IntervalTrigger)
    assert int(triggers[0].interval.total_seconds()) == seconds


def test_interval_has_a_floor():
    """A 1-second schedule would hammer the LLM; clamp to a minute."""
    assert int(parse_schedule("1s")[0].interval.total_seconds()) == 60


# --- cron ---------------------------------------------------------------------------- #

def test_raw_cron_still_works():
    triggers = parse_schedule("0 9 * * *")
    assert len(triggers) == 1
    assert _fields(triggers[0])["hour"] == "9"


def test_step_cron():
    assert _fields(parse_schedule("0 */4 * * *")[0])["hour"] == "*/4"


def test_cron_nicknames():
    assert _fields(parse_schedule("@daily")[0])["hour"] == "0"
    assert isinstance(parse_schedule("@hourly")[0], CronTrigger)


def test_invalid_cron_is_rejected():
    assert parse_schedule("99 99 * * *") == []


# --- combinations -------------------------------------------------------------------- #

def test_interval_plus_a_fixed_time():
    triggers = parse_schedule("every 6h; 08:00 mon")
    assert len(triggers) == 2
    assert isinstance(triggers[0], IntervalTrigger)
    assert _fields(triggers[1])["day_of_week"] == "mon"


def test_newlines_separate_parts():
    assert len(parse_schedule("09:00 mon-fri\nevery 12h")) == 2


def test_one_bad_part_does_not_discard_the_good_ones():
    triggers = parse_schedule("09:00; total nonsense")
    assert len(triggers) == 1


def test_everything_is_utc():
    for trigger in parse_schedule("09:00 mon-fri; every 6h"):
        assert str(trigger.timezone) == "UTC"


# --- back-compat + readback ---------------------------------------------------------- #

def test_parse_trigger_returns_the_first_trigger():
    assert isinstance(parse_trigger("every 6h"), IntervalTrigger)
    assert parse_trigger("nonsense") is None


@pytest.mark.parametrize("text,expected", [
    ("09:00", "at 09:00 UTC"),
    ("9am, 6pm", "at 09:00 and 18:00 UTC — 14× a week"),
    ("09:00 mon-fri", "at 09:00 UTC on mon-fri"),
    ("every 6h", "every 6 hours"),
    ("every 30m", "every 30 minutes"),
])
def test_readback_explains_the_schedule(text, expected):
    assert describe(text) == expected


def test_readback_flags_a_schedule_that_will_never_fire():
    assert "never fire" in describe("gibberish")


def test_readback_of_a_combination_lists_both():
    readback = describe("every 6h; 08:00 mon")
    assert "every 6 hours" in readback and "08:00" in readback


def test_readback_survives_step_cron():
    """int() on "*/4" used to raise while rendering the help text."""
    assert "*/4" in describe("0 */4 * * *")


# --- the comma/semicolon trap --------------------------------------------------------- #
# Reported as unclear, and it is the difference between 6 posts a week and 3:
# a comma builds ONE schedule from every time × every day, a semicolon makes
# each entry its own schedule.

def test_commas_build_one_schedule_from_every_time_and_every_day():
    triggers = parse_schedule("03:00 thu, 03:00 tue, 15:00 sun")
    assert len(triggers) == 1
    assert describe("03:00 thu, 03:00 tue, 15:00 sun") == \
        "at 03:00 and 15:00 UTC on thu,tue,sun — 6× a week"


def test_semicolons_keep_each_entry_separate():
    triggers = parse_schedule("03:00 thu; 03:00 tue; 15:00 sun")
    assert len(triggers) == 3
    assert describe("03:00 thu; 03:00 tue; 15:00 sun") == (
        "at 03:00 UTC on thu · at 03:00 UTC on tue · at 15:00 UTC on sun")


def test_a_repeated_time_is_not_read_back_twice():
    """"at 03:00 and 03:00" reads like a bug rather than like a cross-product."""
    assert describe("03:00 thu, 03:00 tue").count("03:00") == 1


# --- how often it actually fires ------------------------------------------------------ #
# The count is what makes the cross-product visible. Only shown when there is
# more than one time of day, since that is when times multiply across days.

@pytest.mark.parametrize("text,expected", [
    ("09:00, 18:00", "14× a week"),
    ("09:00, 18:00 mon-fri", "10× a week"),
    ("09:00 and 18:00 weekends", "4× a week"),
    ("03:00, 09:00, 15:00 mon,wed", "6× a week"),
])
def test_the_readback_says_how_often_it_fires(text, expected):
    assert expected in describe(text)


@pytest.mark.parametrize("text", ["09:00", "09:00 mon", "every 6h", "0 9 * * *", "daily"])
def test_a_single_daily_time_is_not_annotated(text):
    """"7× a week" on a once-a-day schedule is noise."""
    assert "a week" not in describe(text)


def test_a_step_expression_is_not_counted():
    """A wrong count is worse than none."""
    assert "a week" not in describe("0 */4 * * *")
