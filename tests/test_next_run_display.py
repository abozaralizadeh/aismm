"""What the dashboard shows as "Next run", versus what will actually happen.

The scheduler's next fire time is not the same as the next POST. The
orchestrator skips a ``live`` run whose account is in a publishing cooldown
(``_run_one``), so the dashboard promised "Next run: 08:30 UTC" while the log a
minute later said "Skipping … publishing is rate-limited for another 2h 32m".
These pin the rule the display mirrors — and, just as importantly, the cases
where it must NOT skip ahead.
"""
import datetime as dt

import pytest

from aismm import cooldown
from aismm.dashboard import app as app_module
from aismm.models import Account, Instruction, PlatformName, PublishMode


def _utc(hours_from_now):
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours_from_now)


@pytest.fixture()
def setup(store, monkeypatch):
    """An instruction on one account, with the scheduler's fire times stubbed."""
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="genaicomicbook",
                external_id="1"), access_token="t")
    instruction = store.upsert_instruction(Instruction(
        name="Comicbook", schedule="every 1h", publish_mode=PublishMode.live,
        account_ids_json=f'["{account.id}"]'))

    fires = {"next": _utc(0.5), "after": None}
    monkeypatch.setattr(app_module.scheduler, "next_run_for", lambda _id: fires["next"])
    monkeypatch.setattr(app_module.scheduler, "next_run_after",
                        lambda _id, after: fires["after"])
    return instruction, account, store, fires


def test_a_clear_account_shows_the_real_next_fire(setup):
    instruction, _account, store, fires = setup
    info = app_module._next_run_info(instruction, store)
    assert info["at"] == fires["next"]
    assert info["skipped"] is False


def test_a_cooling_account_shows_when_publishing_actually_resumes(setup):
    """The reported bug: 'Next run 08:30' while the account was blocked till 10:00."""
    instruction, account, store, fires = setup
    cooldown.start(account, store, 3 * 3600)          # blocked for ~3h
    fires["after"] = _utc(3.5)                        # first fire past the cooldown

    info = app_module._next_run_info(instruction, store)
    assert info["skipped"] is True
    assert info["at"] == fires["after"]
    assert info["blocked_until"] is not None


def test_the_blocked_until_time_is_reported_for_the_operator(setup):
    instruction, account, store, _fires = setup
    cooldown.start(account, store, 2 * 3600)
    info = app_module._next_run_info(instruction, store)
    assert info["blocked_until"] > dt.datetime.now(dt.timezone.utc)


def test_a_cooldown_that_clears_before_the_next_fire_changes_nothing(setup):
    """Only fires that would actually be skipped should move the displayed time."""
    instruction, account, store, fires = setup
    fires["next"] = _utc(5)
    cooldown.start(account, store, 3600)              # clears in 1h, fire is in 5h
    info = app_module._next_run_info(instruction, store)
    assert info["skipped"] is False
    assert info["at"] == fires["next"]


def test_dry_run_is_never_skipped_so_it_never_moves(setup):
    """dry_run calls no platform API, so the orchestrator runs it regardless."""
    instruction, account, store, fires = setup
    instruction.publish_mode = PublishMode.dry_run
    store.upsert_instruction(instruction)
    cooldown.start(account, store, 5 * 3600)
    info = app_module._next_run_info(instruction, store)
    assert info["skipped"] is False
    assert info["at"] == fires["next"]


def test_one_free_account_keeps_the_fire_worth_firing(setup):
    """Runs are per account — a single blocked one must not hide the whole fire."""
    instruction, account, store, fires = setup
    free = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="other", external_id="2"),
        access_token="t")
    instruction.set_account_ids([account.id, free.id])
    store.upsert_instruction(instruction)
    cooldown.start(account, store, 5 * 3600)          # only the first is blocked

    info = app_module._next_run_info(instruction, store)
    assert info["skipped"] is False
    assert info["at"] == fires["next"]


def test_an_instruction_with_no_schedule_reports_nothing(setup, monkeypatch):
    instruction, _account, store, _fires = setup
    monkeypatch.setattr(app_module.scheduler, "next_run_for", lambda _id: None)
    info = app_module._next_run_info(instruction, store)
    assert info["at"] is None and info["skipped"] is False


def test_the_page_shows_the_pause_note(store, monkeypatch):
    """End to end through the template, not just the helper."""
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="genaicomicbook",
                external_id="1"), access_token="t")
    instruction = store.upsert_instruction(Instruction(
        name="Comicbook", schedule="every 1h", publish_mode=PublishMode.live,
        account_ids_json=f'["{account.id}"]'))
    cooldown.start(account, store, 3 * 3600)
    monkeypatch.setattr(app_module.scheduler, "next_run_for", lambda _id: _utc(0.5))
    monkeypatch.setattr(app_module.scheduler, "next_run_after", lambda _id, after: _utc(3.5))

    application = app_module.create_app()
    application.secret_key = "test"
    page = application.test_client().get("/instructions").get_data(as_text=True)
    assert "publishing paused until" in page
    assert "earlier fires are skipped" in page


def test_cooldown_deadline_is_none_once_it_expires(store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id="1"), access_token="t")
    assert cooldown.deadline(account) is None
    cooldown.start(account, store, 60)
    assert cooldown.deadline(store.get_account(account.id)) is not None
    cooldown.clear(account, store)
    assert cooldown.deadline(account) is None


# --- lifting a cooldown by hand ------------------------------------------------------ #

@pytest.fixture()
def dash(store, monkeypatch):
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    monkeypatch.setattr(app_module.scheduler, "next_run_for", lambda _id: _utc(0.5))
    monkeypatch.setattr(app_module.scheduler, "next_run_after", lambda _id, after: _utc(3))
    application = app_module.create_app()
    application.secret_key = "test"
    return application


def _cooling(store, hours=3):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="genaicomicbook",
                external_id="1"), access_token="t")
    instruction = store.upsert_instruction(Instruction(
        name="Comicbook", schedule="every 1h", publish_mode=PublishMode.live,
        account_ids_json=f'["{account.id}"]'))
    cooldown.start(account, store, int(hours * 3600))
    return instruction, account


def test_clearing_lifts_the_cooldown(dash, store):
    instruction, account = _cooling(store)
    assert cooldown.is_active(store.get_account(account.id))

    dash.test_client().post(f"/instructions/{instruction.id}/clear-cooldown")
    assert cooldown.is_active(store.get_account(account.id)) is False


def test_clearing_keeps_the_strike_count(dash, store):
    """A human override is not evidence the platform stopped blocking.

    Resetting the streak would restart the backoff at the base duration, so a
    click-then-refused loop would never escalate.
    """
    instruction, account = _cooling(store)
    cooldown.start(store.get_account(account.id), store, 3600)      # now at strike 2
    before = cooldown.strike_count(store.get_account(account.id))

    dash.test_client().post(f"/instructions/{instruction.id}/clear-cooldown")
    assert cooldown.strike_count(store.get_account(account.id)) == before


def test_the_next_refusal_resumes_the_escalation(dash, store):
    """The consequence of keeping the strikes: no restart at 60 minutes."""
    instruction, account = _cooling(store)
    dash.test_client().post(f"/instructions/{instruction.id}/clear-cooldown")

    held = cooldown.start(store.get_account(account.id), store, 3600)
    assert held > 3600, "backoff restarted from the base instead of resuming"


def test_the_button_appears_only_while_a_cooldown_is_active(dash, store):
    instruction, account = _cooling(store)
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert "Clear cooldown" in page

    cooldown.clear(store.get_account(account.id), store)
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert "Clear cooldown" not in page


def test_the_button_warns_before_lifting(dash, store):
    """It can extend a real block, so it must not be a silent one-click action."""
    _instruction, _account = _cooling(store)
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert "confirm(" in page
    assert "can extend the block" in page


def test_clearing_a_clean_instruction_says_so(dash, store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="x", external_id="1"),
        access_token="t")
    instruction = store.upsert_instruction(Instruction(
        name="Clean", publish_mode=PublishMode.live,
        account_ids_json=f'["{account.id}"]'))
    response = dash.test_client().post(
        f"/instructions/{instruction.id}/clear-cooldown", follow_redirects=True)
    assert b"No active cooldown" in response.data


def test_clearing_an_unknown_instruction_is_404(dash):
    assert dash.test_client().post("/instructions/nope/clear-cooldown").status_code == 404


def test_a_dry_run_instruction_still_shows_its_cooling_account(dash, store):
    """dry_run never skips, but it shares the account with live instructions."""
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="shared", external_id="1"),
        access_token="t")
    instruction = store.upsert_instruction(Instruction(
        name="Preview", publish_mode=PublishMode.dry_run,
        account_ids_json=f'["{account.id}"]'))
    cooldown.start(account, store, 3600)

    info = app_module._next_run_info(store.get_instruction(instruction.id), store)
    assert info["skipped"] is False          # dry_run is not blocked...
    assert info["cooling"]                   # ...but the cooldown is still visible
