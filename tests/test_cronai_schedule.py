"""Schedules written in the cronai widget, and run by our scheduler.

The instruction page embeds the hosted ``<cron-ai>`` widget, which shows the
operator the next runs it computed. Our scheduler must fire at exactly those
moments, so the first test replays ``tests/fixtures/cronai_runs.json``, generated
from the HOSTED engine by ``scripts/make_cronai_fixtures.mjs``: every phrase,
time zone and start point (two DST weekends among them) must agree.
"""
import dataclasses
import datetime as dt
import json
from pathlib import Path

import pytest

from aismm import schedules
from aismm.models import Instruction
from aismm.schedules import (CronLine, CronLinesTrigger, ScheduleError, describe, from_text,
                             from_widget, label, parse_schedule, read_saved)

UTC = dt.timezone.utc
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "cronai_runs.json").read_text())


def _runs(trigger, start: dt.datetime, count: int) -> list[str]:
    """``count`` fires strictly after ``start`` (cronai's convention), as UTC ISO."""
    out, fire = [], trigger.get_next_fire_time(None, start + dt.timedelta(microseconds=1))
    while fire and len(out) < count:
        out.append(fire.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        fire = trigger.get_next_fire_time(fire, fire)
    return out


def _saved(text="every day at 9am", crons=("0 9 * * *",), timezone="Europe/Rome", **extra):
    return json.dumps({"v": 1, "text": text, "crons": list(crons), "timezone": timezone,
                       "description": "At 09:00 every day", **extra})


def _at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


# --- the widget and the scheduler agree --------------------------------------------- #

@pytest.mark.parametrize("case", FIXTURE["cases"],
                         ids=lambda c: f"{c['schedule']['text']}|{c['schedule']['timezone']}")
def test_fires_exactly_when_the_widget_says(case):
    trigger = read_saved(json.dumps(case["schedule"])).trigger()
    for start, expected in case["runs"].items():
        assert _runs(trigger, _at(start), len(expected)) == expected, start


def test_the_fixture_covers_what_matters():
    crons = {c for case in FIXTURE["cases"] for c in case["schedule"]["crons"]}
    assert {"0 18 * * 5L", "0 9 * * 1#2", "0 0 L * *", "0 9 1 * 1", "30 2 * * *"} <= crons
    assert any(case["schedule"].get("everyWeeks") for case in FIXTURE["cases"])
    assert "abozaralizadeh.github.io/cronai" in FIXTURE["source"]


# --- the cron grammar ----------------------------------------------------------------- #

def _days(cron, start="2026-10-04T00:00:00Z", count=7, zone="UTC"):
    trigger = CronLinesTrigger([cron], zone)
    return [_at(t).strftime("%a %d") for t in _runs(trigger, _at(start), count)]


def test_weekdays_are_standard_cron_where_apscheduler_is_not():
    assert _days("0 9 * * 4", count=2) == ["Thu 08", "Thu 15"]   # APScheduler: Friday
    assert _days("0 9 * * 0", count=1) == _days("0 9 * * 7", count=1) == ["Sun 04"]


def test_a_range_ending_in_7_includes_sunday():
    """Standard cron (7 is Sunday). cronai's own engine drops the Sunday here;
    that is reported upstream, and the widget never emits such a range itself."""
    assert _days("0 9 * * 1-7") == ["Sun 04", "Mon 05", "Tue 06", "Wed 07", "Thu 08",
                                    "Fri 09", "Sat 10"]
    assert _days("0 9 * * 5-7", count=3) == ["Sun 04", "Fri 09", "Sat 10"]


def test_day_of_month_or_weekday():
    """Both restricted: EITHER matches (classic cron). APScheduler requires both."""
    assert _days("0 9 1 * 1", start="2026-10-25T00:00:00Z", count=4) == [
        "Mon 26", "Sun 01", "Mon 02", "Mon 09"]


@pytest.mark.parametrize("cron,expected", [
    ("0 18 * * 5L", ["Fri 30", "Fri 27", "Fri 25"]),        # last Friday
    ("0 9 * * 1#2", ["Mon 12", "Mon 09", "Mon 14"]),        # second Monday
    ("0 0 L * *", ["Sat 31", "Mon 30", "Thu 31"]),          # last day of the month
    ("0 9 * * FRI-MON", ["Sun 04", "Mon 05", "Fri 09"]),    # a range that wraps
])
def test_extensions(cron, expected):
    assert _days(cron, count=3) == expected


def test_a_time_inside_the_dst_gap_is_skipped_and_an_overlap_fires_once():
    rome = "Europe/Rome"
    spring = _days("30 2 * * *", start="2027-03-26T12:00:00Z", count=3, zone=rome)
    assert spring == ["Sat 27", "Mon 29", "Tue 30"]          # 28 March has no 02:30
    trigger = CronLinesTrigger(["30 2 * * *"], rome)
    autumn = _runs(trigger, _at("2026-10-24T12:00:00Z"), 2)
    assert autumn == ["2026-10-25T00:30:00Z", "2026-10-26T01:30:00Z"]   # the FIRST 02:30


def test_lines_that_share_a_moment_fire_once():
    trigger = CronLinesTrigger(["0 9 * * *", "0 9 * * 1"], "UTC")
    assert len(set(_runs(trigger, _at("2026-10-04T10:00:00Z"), 5))) == 5


def test_every_other_week():
    """cronai's week guard: a weekly line, kept only on every N-th week from the anchor."""
    anchor = 1791140400              # what the widget stored for "every other friday"
    trigger = CronLinesTrigger(["0 17 * * 5"], "Europe/Rome", every_weeks=2,
                               week_anchor=anchor)
    fires = _runs(trigger, _at("2026-10-04T10:00:00Z"), 3)
    gaps = {(_at(b) - _at(a)).days for a, b in zip(fires, fires[1:])}
    assert gaps == {14}


@pytest.mark.parametrize("cron", ["0 9 * *", "61 9 * * *", "0 9 * * 8", "0 9 * * L",
                                  "0 9 ,1 * *", "*/0 * * * *", "0 9 * * MOON"])
def test_bad_lines_are_refused(cron):
    with pytest.raises(ScheduleError):
        CronLine(cron)


def test_start_date_still_gates_the_first_fire():
    start = dt.datetime(2026, 10, 20, tzinfo=UTC)
    trigger = read_saved(_saved()).trigger(start_date=start)
    assert trigger.get_next_fire_time(None, _at("2026-10-04T10:00:00Z")) >= start


# --- what is stored -------------------------------------------------------------------- #

@pytest.mark.parametrize("value,reason", [
    ("{not json", "not valid JSON"),
    (json.dumps({"v": 2, "text": "x", "crons": ["0 9 * * *"]}), "version 1"),
    (_saved(crons=[]), "no cron lines"),
    (_saved(text=" "), "no readable text"),
    (_saved(crons=["0 9 * *"]), "5 fields"),
    (_saved(timezone="Mars/Olympus"), "unknown time zone"),
    (_saved(everyWeeks=2), "week anchor"),
    (_saved(everyWeeks=99, anchor=1), "between 1 and 52"),
])
def test_a_bad_saved_schedule_is_refused_with_a_reason(value, reason):
    with pytest.raises(ScheduleError, match=reason):
        read_saved(value)


def test_one_saved_schedule_is_one_trigger_and_one_job():
    triggers = parse_schedule(_saved(crons=["0 */3 * * *", "30 1-22/3 * * *"]))
    assert len(triggers) == 1 and isinstance(triggers[0], CronLinesTrigger)


def test_an_unreadable_saved_schedule_never_fires_and_says_so():
    assert parse_schedule('{"v": 1}') == []
    assert "never fire" in describe('{"v": 1}')


def test_readback_and_label():
    saved = _saved()
    assert describe(saved) == "At 09:00 every day (Europe/Rome)"
    assert label(saved) == "every day at 9am"
    assert label("09:00 mon-fri") == "09:00 mon-fri"


# --- saving the page ------------------------------------------------------------------- #

LEGACY = "06:00 thu; 06:00 tue; 6:00 sun"


def test_an_untouched_older_schedule_is_kept_even_when_the_widget_cannot_read_it():
    """The widget posts "" for words it does not understand. Saving some other
    field must not wipe a schedule written in the older grammar."""
    assert from_widget("", typed=LEGACY, shown=LEGACY, current=LEGACY) == LEGACY


def test_an_untouched_older_schedule_is_kept_when_the_widget_reads_it_in_utc():
    """It runs as before (an interval keeps its phase), not as re-derived cron."""
    posted = _saved(text="every 90m", crons=["0 */3 * * *", "30 1-22/3 * * *"], timezone="UTC")
    assert from_widget(posted, typed="every 90m", shown="every 90m",
                       current="every 90m") == "every 90m"


def test_moving_an_older_schedule_to_another_time_zone_converts_it():
    posted = _saved(text="09:00", crons=["0 9 * * *"], timezone="Europe/Rome")
    stored = from_widget(posted, typed="09:00", shown="09:00", current="09:00")
    assert read_saved(stored).timezone == "Europe/Rome"


def test_new_words_are_stored_as_the_widget_saved_them():
    posted = _saved(text="every other friday at 5pm", crons=["0 17 * * 5"],
                    everyWeeks=2, anchor=1791140400)
    stored = read_saved(from_widget(posted, typed="every other friday at 5pm",
                                    shown=LEGACY, current=LEGACY))
    assert (stored.crons, stored.every_weeks, stored.anchor) == (("0 17 * * 5",), 2, 1791140400)


def test_an_untouched_widget_schedule_saves_what_the_box_shows():
    """The operator saw the widget's CURRENT reading when they pressed Save. If a
    widget fix makes the same words mean something else, that is what they saw."""
    words = "at 2:30 every night"
    shown = _saved(text=words, crons=["30 14 * * *"])        # an older engine's reading
    posted = _saved(text=words, crons=["30 2 * * *"])        # what the box shows today
    stored = from_widget(posted, typed=words, shown=shown, current=shown)
    assert read_saved(stored).crons == ("30 2 * * *",)


def test_an_emptied_box_unschedules():
    assert from_widget("", typed="", shown=LEGACY, current=LEGACY) == ""


def test_changed_words_that_were_not_understood_are_refused():
    with pytest.raises(ScheduleError, match="not understood"):
        from_widget("", typed="blorp", shown=LEGACY, current=LEGACY)


def test_without_the_page_script_an_empty_value_keeps_the_schedule():
    assert from_widget("", typed=None, shown=LEGACY, current=LEGACY) == LEGACY


def test_the_text_box_validates_pasted_widget_json():
    assert from_text("09:00 mon-fri", current="") == "09:00 mon-fri"
    with pytest.raises(ScheduleError):
        from_text('{"v": 1, "text": "x"}', current="")


# --- the dashboard ---------------------------------------------------------------------- #

@pytest.fixture()
def client(store, monkeypatch, tmp_path):
    from aismm import assets as assets_module
    from aismm import config as config_module
    from aismm.config import AuthSettings
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    (tmp_path / "assets").mkdir(exist_ok=True)
    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module, assets_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    store.init()
    application = app_module.create_app()
    application.secret_key = "test"
    return application.test_client()


def _form(instr, **extra):
    data = {"id": instr.id, "name": instr.name, "brief": "b", "publish_mode": "dry_run",
            "media_pref": "auto", "task_type": "publish"}
    data.update(extra)
    return data


def test_the_page_loads_the_hosted_widget_pages_first(client):
    page = client.get("/instructions/new").get_data(as_text=True)
    assert "<cron-ai" in page and 'name="schedule_json"' in page
    pages = page.index("https://abozaralizadeh.github.io/cronai/widget/cronai-widget.js")
    cdn = page.index("https://cdn.jsdelivr.net/gh/abozaralizadeh/cronai@master/")
    assert pages < cdn, "GitHub Pages first: jsDelivr caches @master for a week"
    assert "schedule-help" not in page            # the old cheat-sheet is gone
    fallback = page[page.index("data-schedule-fallback"):]
    assert 'name="schedule"' in fallback[:900]    # the text box stays as the fallback


def test_a_saved_schedule_is_restored_whole(client, store):
    instr = store.upsert_instruction(Instruction(name="I", brief="b", schedule=_saved()))
    page = client.get(f"/instructions/{instr.id}/edit").get_data(as_text=True)
    tag = page[page.index("<cron-ai"):page.index("</cron-ai>")]
    assert "schedule=" in tag and "&#34;crons&#34;" in tag and 'timezone="UTC"' not in tag
    assert "At 09:00 every day (Europe/Rome)" in page


def test_an_older_schedule_is_shown_as_words_in_utc(client, store):
    instr = store.upsert_instruction(Instruction(name="I", brief="b", schedule=LEGACY))
    page = client.get(f"/instructions/{instr.id}/edit").get_data(as_text=True)
    tag = page[page.index("<cron-ai"):page.index("</cron-ai>")]
    assert f'value="{LEGACY}"' in tag and 'timezone="UTC"' in tag
    assert "Saved in the older format" in page


def test_saving_the_widget_stores_its_schedule(client, store):
    instr = store.upsert_instruction(Instruction(name="I", brief="b", schedule=LEGACY))
    posted = _saved(text="every day at 9am")
    client.post("/instructions", data=_form(instr, schedule_json=posted,
                                            schedule_typed="every day at 9am",
                                            schedule_shown=LEGACY, schedule=LEGACY))
    assert read_saved(store.get_instruction(instr.id).schedule).text == "every day at 9am"


def test_saving_another_field_keeps_an_older_schedule(client, store):
    instr = store.upsert_instruction(Instruction(name="I", brief="b", schedule=LEGACY))
    client.post("/instructions", data=_form(instr, brief="new brief", schedule_json="",
                                            schedule_typed=LEGACY, schedule_shown=LEGACY))
    saved = store.get_instruction(instr.id)
    assert (saved.brief, saved.schedule) == ("new brief", LEGACY)


def test_words_not_understood_leave_the_schedule_alone_and_say_why(client, store):
    instr = store.upsert_instruction(Instruction(name="I", brief="b", schedule=LEGACY))
    response = client.post("/instructions", follow_redirects=True,
                           data=_form(instr, schedule_json="", schedule_typed="blorp",
                                      schedule_shown=LEGACY))
    assert store.get_instruction(instr.id).schedule == LEGACY
    assert "Schedule not changed" in response.get_data(as_text=True)


def test_the_list_shows_the_words_not_the_json(client, store):
    store.upsert_instruction(Instruction(name="I", brief="b", schedule=_saved()))
    page = client.get("/instructions").get_data(as_text=True)
    assert "every day at 9am" in page and "&#34;crons&#34;" not in page
    assert "every day at 9am" in client.get("/instructions?q=9am").get_data(as_text=True)


def test_rate_limit_advice_counts_real_fires():
    from types import SimpleNamespace

    from aismm.tools.publish_tool import _schedule_advice

    hourly = _saved(text="every hour", crons=["0 * * * *"])
    assert "about 24 times a day" in _schedule_advice(SimpleNamespace(schedule=hourly))
    assert _schedule_advice(SimpleNamespace(schedule=_saved())) == ""
    assert schedules.fires_per_day("every 2h") == pytest.approx(12, abs=0.2)


def test_the_widget_text_box_is_16px_on_touch():
    """iOS zooms into a field under 16px. The widget's box is in its shadow DOM,
    where the dashboard's input rule cannot reach; ::part(input) can."""
    css = (Path(__file__).parents[1] / "aismm" / "dashboard" / "static" / "style.css").read_text()
    coarse = css[css.index("@media (pointer: coarse)"):]
    assert "cron-ai::part(input) { font-size: 16px; }" in coarse[:coarse.index("\n}\n")]
