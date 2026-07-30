"""Interval phase, the optional start time, and the "next run" readout.

The bug: ``IntervalTrigger`` anchors to the moment it is CONSTRUCTED unless given
a ``start_date``. ``refresh_jobs`` rebuilds every trigger from scratch — on every
service restart and on every dashboard save of ANY instruction — so an
"every 1h" schedule silently pushed its next fire a full hour into the future
each time, and a frequently-edited deployment could starve an instruction
indefinitely without a single line in the log.

The fix is a stable anchor (the operator's explicit start, else ``created_at``),
which is also the thing that makes an optional "starts at" meaningful.
"""
import datetime as dt

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from aismm import scheduler as scheduler_module
from aismm.models import Instruction
from aismm.schedules import describe, parse_schedule, parse_trigger

UTC = dt.timezone.utc
ANCHOR = dt.datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _next(trigger, now=None):
    return trigger.get_next_fire_time(None, now or dt.datetime.now(UTC))


# --- the drift itself ---------------------------------------------------------------- #

def test_the_same_anchor_yields_the_same_next_fire(store):
    """Rebuilding a trigger must not move it. This is the whole bug."""
    first = parse_trigger("every 1h", anchor=ANCHOR)
    second = parse_trigger("every 1h", anchor=ANCHOR)
    now = dt.datetime.now(UTC)
    assert _next(first, now) == _next(second, now)


def test_an_anchored_interval_lands_on_the_anchor_phase():
    """Anchored at midnight, an hourly job fires on the hour — not at a random offset."""
    trigger = parse_trigger("every 1h", anchor=ANCHOR)
    fire = _next(trigger, dt.datetime(2026, 3, 3, 14, 37, tzinfo=UTC))
    assert (fire.minute, fire.second) == (0, 0)
    assert fire == dt.datetime(2026, 3, 3, 15, 0, tzinfo=UTC)


def test_a_90_minute_interval_keeps_its_phase():
    trigger = parse_trigger("every 90 minutes", anchor=ANCHOR)
    now = dt.datetime(2026, 3, 3, 14, 37, tzinfo=UTC)
    first = _next(trigger, now)
    # Every fire sits on the anchor's 90-minute grid...
    assert (first - ANCHOR).total_seconds() % (90 * 60) == 0
    # ...and the one after it is exactly an interval later.
    assert trigger.get_next_fire_time(first, first) - first == dt.timedelta(minutes=90)


def test_without_an_anchor_the_phase_moves(monkeypatch):
    """Documents the old behaviour, so a regression is unmistakable."""
    import time

    first = parse_trigger("every 1h")
    time.sleep(0.05)
    second = parse_trigger("every 1h")
    assert first.start_date != second.start_date


# --- the optional start -------------------------------------------------------------- #

def test_a_future_start_delays_the_first_interval_fire():
    start = dt.datetime.now(UTC) + dt.timedelta(days=3)
    trigger = parse_trigger("every 1h", anchor=start)
    assert _next(trigger) >= start


def test_a_future_start_delays_a_time_of_day_schedule():
    """Cron doesn't drift, but a start date still gates when it begins."""
    start = dt.datetime.now(UTC) + dt.timedelta(days=3)
    trigger = parse_trigger("09:00", anchor=start)
    assert isinstance(trigger, CronTrigger)
    assert _next(trigger) >= start


def test_a_past_start_does_not_change_a_cron_schedule():
    """An anchor already behind us must be a no-op for time-of-day schedules."""
    now = dt.datetime(2026, 3, 3, 14, 37, tzinfo=UTC)
    assert _next(parse_trigger("09:00", anchor=ANCHOR), now) == \
        _next(parse_trigger("09:00"), now)


def test_raw_cron_accepts_an_anchor():
    """from_crontab has no start_date parameter — we build the trigger by hand."""
    start = dt.datetime.now(UTC) + dt.timedelta(days=2)
    trigger = parse_trigger("0 9 * * *", anchor=start)
    assert isinstance(trigger, CronTrigger)
    assert _next(trigger) >= start


def test_cron_nicknames_accept_an_anchor():
    start = dt.datetime.now(UTC) + dt.timedelta(days=2)
    assert _next(parse_trigger("@daily", anchor=start)) >= start


def test_named_cadences_accept_an_anchor():
    start = dt.datetime.now(UTC) + dt.timedelta(days=2)
    assert _next(parse_trigger("hourly", anchor=start)) >= start
    assert _next(parse_trigger("weekly", anchor=start)) >= start


def test_a_multi_trigger_schedule_anchors_every_part():
    start = dt.datetime.now(UTC) + dt.timedelta(days=2)
    triggers = parse_schedule("every 6h; 08:00 mon", anchor=start)
    assert len(triggers) == 2
    assert all(_next(t) >= start for t in triggers)


def test_no_anchor_still_parses_everything():
    """The parameter is optional — old callers keep working."""
    assert isinstance(parse_trigger("every 6h"), IntervalTrigger)
    assert isinstance(parse_trigger("09:00"), CronTrigger)
    assert parse_trigger("nonsense") is None


# --- the readback --------------------------------------------------------------------- #

def test_describe_mentions_an_explicit_start():
    start = dt.datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
    assert "starting 2026-08-05 09:00 UTC" in describe("every 1h", starts_at=start)


def test_describe_without_a_start_is_unchanged():
    assert describe("every 1h") == "every 1 hour"
    assert "starting" not in describe("09:00")


def test_describe_still_reports_an_unparseable_schedule():
    assert "never fire" in describe("nonsense", starts_at=dt.datetime.now(UTC))


def test_describe_renders_90_minutes():
    assert describe("every 90 minutes") == "every 90 minutes"


# --- the scheduler keeps the phase across a refresh ----------------------------------- #

@pytest.fixture()
def live_scheduler(store, monkeypatch):
    """A real BackgroundScheduler wired to a throwaway store, shut down after."""
    monkeypatch.setattr(scheduler_module, "get_store", lambda: store)
    monkeypatch.setattr(scheduler_module, "_scheduler", None)
    sched = scheduler_module.get_scheduler()
    sched.start()
    try:
        yield sched
    finally:
        sched.shutdown(wait=False)
        monkeypatch.setattr(scheduler_module, "_scheduler", None)


def test_refreshing_jobs_does_not_move_an_interval(store, live_scheduler):
    """Saving ANY instruction rebuilds every job — that must not re-base the phase."""
    store.upsert_instruction(Instruction(name="Hourly", schedule="every 1h"))
    scheduler_module.refresh_jobs()
    before = scheduler_module.next_run_for(store.list_instructions()[0].id)

    scheduler_module.refresh_jobs()          # e.g. an unrelated instruction was saved
    after = scheduler_module.next_run_for(store.list_instructions()[0].id)
    assert before == after


def test_next_run_for_reports_the_earliest_of_several_triggers(store, live_scheduler):
    instr = store.upsert_instruction(
        Instruction(name="Twice", schedule="every 6h; 08:00 mon"))
    scheduler_module.refresh_jobs()

    jobs = [j for j in live_scheduler.get_jobs() if j.id.startswith(f"instr:{instr.id}")]
    assert len(jobs) == 2
    assert scheduler_module.next_run_for(instr.id) == min(j.next_run_time for j in jobs)


def test_next_run_for_an_unscheduled_instruction_is_none(store, live_scheduler):
    instr = store.upsert_instruction(Instruction(name="Manual", schedule=""))
    scheduler_module.refresh_jobs()
    assert scheduler_module.next_run_for(instr.id) is None


def test_an_explicit_start_pushes_the_next_run_out(store, live_scheduler):
    start = dt.datetime.now(UTC) + dt.timedelta(days=2)
    instr = store.upsert_instruction(
        Instruction(name="Later", schedule="every 1h", schedule_start_at=start))
    scheduler_module.refresh_jobs()
    nxt = scheduler_module.next_run_for(instr.id)
    assert nxt and nxt >= start


def test_next_run_for_is_none_when_the_scheduler_is_not_running(store, monkeypatch):
    """Dashboard-only mode (AISMM_ENABLE_SCHEDULER=0) must not raise."""
    monkeypatch.setattr(scheduler_module, "_scheduler", None)
    assert scheduler_module.next_run_for("anything") is None


# --- the dashboard field --------------------------------------------------------------- #

@pytest.fixture()
def dash(store, monkeypatch):
    from aismm.dashboard import app as app_module

    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


def test_the_form_offers_a_start_field(dash, store):
    instr = store.upsert_instruction(Instruction(name="X", schedule="every 1h"))
    page = dash.test_client().get(
        f"/instructions/{instr.id}/edit").get_data(as_text=True)
    assert 'name="schedule_start_at"' in page
    assert 'type="datetime-local"' in page


def test_an_existing_start_is_prefilled(dash, store):
    instr = store.upsert_instruction(Instruction(
        name="X", schedule="every 1h",
        schedule_start_at=dt.datetime(2026, 8, 5, 9, 0, tzinfo=UTC)))
    page = dash.test_client().get(
        f"/instructions/{instr.id}/edit").get_data(as_text=True)
    assert 'value="2026-08-05T09:00"' in page


def test_saving_a_start_stores_it_as_utc(dash, store):
    instr = store.upsert_instruction(Instruction(name="X", schedule="every 1h"))
    dash.test_client().post("/instructions", data={
        "id": instr.id, "name": "X", "brief": "", "schedule": "every 1h",
        "schedule_start_at": "2026-09-01T08:30", "publish_mode": "dry_run",
        "media_pref": "auto", "enabled": "on"})
    stored = store.get_instruction(instr.id).schedule_start_at
    assert (stored.year, stored.month, stored.day) == (2026, 9, 1)
    assert (stored.hour, stored.minute) == (8, 30)


def test_clearing_the_field_removes_the_start(dash, store):
    instr = store.upsert_instruction(Instruction(
        name="X", schedule="every 1h",
        schedule_start_at=dt.datetime(2026, 8, 5, 9, 0, tzinfo=UTC)))
    dash.test_client().post("/instructions", data={
        "id": instr.id, "name": "X", "brief": "", "schedule": "every 1h",
        "schedule_start_at": "", "publish_mode": "dry_run",
        "media_pref": "auto", "enabled": "on"})
    assert store.get_instruction(instr.id).schedule_start_at is None


def test_a_malformed_start_is_ignored_not_fatal(dash, store):
    """A hand-edited form value must not 500 the save."""
    instr = store.upsert_instruction(Instruction(name="X", schedule="every 1h"))
    response = dash.test_client().post("/instructions", data={
        "id": instr.id, "name": "X", "brief": "", "schedule": "every 1h",
        "schedule_start_at": "not-a-date", "publish_mode": "dry_run",
        "media_pref": "auto", "enabled": "on"})
    assert response.status_code in (200, 302)
    assert store.get_instruction(instr.id).schedule_start_at is None


def test_the_list_page_has_a_next_run_column(dash, store):
    store.upsert_instruction(Instruction(name="X", schedule="every 1h"))
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert "Next run" in page


def test_the_readback_shows_the_start(dash, store):
    instr = store.upsert_instruction(Instruction(
        name="X", schedule="every 1h",
        schedule_start_at=dt.datetime(2026, 8, 5, 9, 0, tzinfo=UTC)))
    page = dash.test_client().get(
        f"/instructions/{instr.id}/edit").get_data(as_text=True)
    assert "starting 2026-08-05 09:00 UTC" in page
