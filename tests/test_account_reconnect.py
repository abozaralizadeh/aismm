"""Reconnecting an account must UPDATE it, never add a second one.

Reported live: reconnecting one Instagram account (comicbook) "recreated the
whole connections that are managed through my facebook account and duplicated
all of them" — 8 accounts where there should have been 5.

`upsert_account` keys on the row id, and the OAuth callback built a fresh
`Account()` every time, minting a new uuid. So every reconnect added a row.

That is worse than untidy. An instruction stores account IDs, so after a
reconnect its instructions still pointed at the OLD rows — whose tokens the
re-authorization had just invalidated. The account looked connected, the
instruction looked configured, and publishing had silently stopped.
"""
import dataclasses
import datetime as dt

import pytest

from aismm import config as config_module
from aismm.config import AuthSettings
from aismm.models import Account, Instruction, PlatformName
from aismm.platforms.base import Identity, TokenBundle


@pytest.fixture()
def dash(monkeypatch, store, tmp_path):
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


@pytest.fixture()
def connect(monkeypatch):
    """Drive the OAuth callback with a fixed set of identities."""
    from aismm.platforms.instagram import Instagram

    state = {"identities": [], "token": "fresh-token"}

    async def exchange(_self, **kw):
        return TokenBundle(access_token=state["token"], refresh_token="r",
                           expires_in=3600, scope="instagram_basic")

    async def identities(_self, _token):
        return state["identities"]

    monkeypatch.setattr(Instagram, "exchange_code", exchange)
    monkeypatch.setattr(Instagram, "fetch_identities", identities)
    return state


def _callback(dash, platform="instagram"):
    client = dash.test_client()
    with client.session_transaction() as session:
        session[f"oauth_state_{platform}"] = "s"
        session[f"oauth_ws_{platform}"] = ""
    return client.get(f"/oauth/{platform}/callback?state=s&code=c", follow_redirects=True)


def _identity(external_id, handle, **meta):
    return Identity(external_id=external_id, handle=handle,
                    meta={"access_token": "page-token", **meta})


# --- the bug ---------------------------------------------------------------------------- #

def test_reconnecting_updates_the_account_instead_of_adding_one(dash, store, connect):
    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)
    assert len(store.list_accounts()) == 1

    _callback(dash)                                  # the operator reconnects
    accounts = store.list_accounts()
    assert len(accounts) == 1, "a reconnect must not duplicate the account"


def test_a_meta_login_covering_three_accounts_does_not_triple_them(dash, store, connect):
    """One Meta login claims every linked Page, so reconnecting ONE account
    re-authorizes all of them — which is how 5 accounts became 8."""
    connect["identities"] = [_identity("1", "genaicomicbook"),
                             _identity("2", "apadana.audiology.clinic"),
                             _identity("3", "emortezaei")]
    _callback(dash)
    _callback(dash)
    assert len(store.list_accounts()) == 3


def test_the_account_id_survives_so_instructions_keep_working(dash, store, connect):
    """The silent half of the bug: an instruction points at an account ID."""
    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)
    account = store.list_accounts()[0]

    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([account.id])
    store.upsert_instruction(instruction)

    _callback(dash)
    assert store.list_accounts()[0].id == account.id
    still = store.get_instruction(instruction.id)
    assert still.account_ids == [account.id]


def test_the_fresh_token_is_stored(dash, store, connect):
    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)
    connect["token"] = "second-token"
    _callback(dash)
    account = store.list_accounts()[0]
    assert store.get_tokens(account.id)[0] == "page-token"   # platform-supplied wins


def test_operator_settings_survive_a_reconnect(dash, store, connect):
    """The X community list, the share-with-followers switch, the publish ledger
    and the cooldown all live in meta and cannot be re-derived from a callback."""
    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)
    account = store.list_accounts()[0]
    meta = dict(account.meta)
    meta.update({"community_ids": ["123"], "share_with_followers": True})
    account.set_meta(meta)
    store.upsert_account(account)

    _callback(dash)
    after = store.list_accounts()[0].meta
    assert after["community_ids"] == ["123"]
    assert after["share_with_followers"] is True


def test_a_renamed_handle_is_picked_up(dash, store, connect):
    connect["identities"] = [_identity("1784141", "old_name")]
    _callback(dash)
    connect["identities"] = [_identity("1784141", "new_name")]
    _callback(dash)
    accounts = store.list_accounts()
    assert len(accounts) == 1 and accounts[0].handle == "new_name"


def test_a_genuinely_different_account_is_still_added(dash, store, connect):
    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)
    connect["identities"] = [_identity("9999999", "another.brand")]
    _callback(dash)
    assert len(store.list_accounts()) == 2


def test_the_flash_says_reconnected_not_connected(dash, store, connect):
    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)
    page = _callback(dash).get_data(as_text=True)
    assert "Reconnected instagram" in page


# --- cleaning up the duplicates already in the database --------------------------------- #

def _dupe(store, days_ago, external_id="1784141"):
    account = Account(platform=PlatformName.instagram, handle="genaicomicbook",
                      external_id=external_id)
    account.created_at = dt.datetime.now() - dt.timedelta(days=days_ago)
    return store.upsert_account(account, access_token="t")


def test_pruning_keeps_the_newest_copy_of_each(dash, store):
    old = _dupe(store, 30)
    newest = _dupe(store, 0)
    _dupe(store, 5, external_id="other")

    dash.test_client().post("/accounts/prune-duplicates", follow_redirects=True)
    remaining = {a.id for a in store.list_accounts()}
    assert newest.id in remaining
    assert old.id not in remaining
    assert len(remaining) == 2


def test_pruning_repoints_an_instruction_at_the_surviving_account(dash, store):
    """Deleting the row an instruction targets would break it for good."""
    old = _dupe(store, 30)
    newest = _dupe(store, 0)
    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([old.id])
    store.upsert_instruction(instruction)

    dash.test_client().post("/accounts/prune-duplicates", follow_redirects=True)
    assert store.get_instruction(instruction.id).account_ids == [newest.id]


def test_pruning_does_not_duplicate_an_instruction_targeting_both(dash, store):
    old = _dupe(store, 30)
    newest = _dupe(store, 0)
    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([old.id, newest.id])
    store.upsert_instruction(instruction)

    dash.test_client().post("/accounts/prune-duplicates", follow_redirects=True)
    assert store.get_instruction(instruction.id).account_ids == [newest.id]


def test_pruning_with_nothing_to_do_says_so(dash, store):
    _dupe(store, 3)
    page = dash.test_client().post("/accounts/prune-duplicates",
                                   follow_redirects=True).get_data(as_text=True)
    assert "No duplicate accounts" in page
    assert len(store.list_accounts()) == 1


# --- the page itself --------------------------------------------------------------------- #

def test_the_page_flags_the_duplicates(dash, store):
    _dupe(store, 30)
    _dupe(store, 0)
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "duplicate account row(s)" in page
    assert "superseded" in page


def test_every_account_offers_a_reconnect_button(dash, store):
    """Without it the only way back was the connect grid, where picking the wrong
    app connects a different thing."""
    account = _dupe(store, 1)
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert f"/accounts/{account.id}/reconnect" in page


def test_reconnect_reuses_the_app_that_connected_the_account(dash, store):
    account = _dupe(store, 1)
    account.set_meta({"app_id": "app-123"})
    store.upsert_account(account)
    response = dash.test_client().post(f"/accounts/{account.id}/reconnect")
    assert response.status_code == 302
    assert "app=app-123" in response.headers["Location"]


def test_the_page_says_which_instructions_use_an_account(dash, store):
    account = _dupe(store, 1)
    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([account.id])
    store.upsert_instruction(instruction)
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "1 instruction(s)" in page


# --- a reconnect adopts the orphans it finds --------------------------------------------- #
# Keeping the row id is enough when there is only one row. But an operator who
# already has duplicates from before that fix has instructions pointing at the
# OLD rows — and reconnecting updates the newest, leaving those instructions
# aimed at a row the new authorization has just invalidated. So a reconnect
# repairs the damage rather than stepping around it.

def test_reconnecting_moves_instructions_off_the_stale_duplicate(dash, store, connect):
    old = _dupe(store, 30)
    newest = _dupe(store, 1)
    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([old.id])
    store.upsert_instruction(instruction)

    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)

    assert store.get_instruction(instruction.id).account_ids == [newest.id]


def test_the_row_the_instruction_lands_on_is_the_one_that_was_refreshed(dash, store, connect):
    """Repointing at a row nobody re-authorized would fix nothing."""
    old = _dupe(store, 30)
    newest = _dupe(store, 1)
    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([old.id])
    store.upsert_instruction(instruction)

    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)

    landed = store.get_instruction(instruction.id).account_ids[0]
    assert landed == newest.id
    assert store.get_tokens(landed)[0] == "page-token"      # the fresh token


def test_an_instruction_targeting_both_rows_does_not_end_up_with_two(dash, store, connect):
    old = _dupe(store, 30)
    newest = _dupe(store, 1)
    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([old.id, newest.id])
    store.upsert_instruction(instruction)

    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)

    assert store.get_instruction(instruction.id).account_ids == [newest.id]


def test_other_accounts_on_the_instruction_are_left_alone(dash, store, connect):
    """A multi-platform instruction must keep its other targets."""
    old = _dupe(store, 30)
    newest = _dupe(store, 1)
    other = store.upsert_account(
        Account(platform=PlatformName.twitter, handle="abo0zar", external_id="9"),
        access_token="t")
    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([old.id, other.id])
    store.upsert_instruction(instruction)

    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)

    assert store.get_instruction(instruction.id).account_ids == [newest.id, other.id]


def test_a_clean_reconnect_touches_no_instruction(dash, store, connect):
    """Nothing to adopt, nothing to rewrite."""
    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)
    account = store.list_accounts()[0]
    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([account.id])
    store.upsert_instruction(instruction)

    page = _callback(dash).get_data(as_text=True)
    assert store.get_instruction(instruction.id).account_ids == [account.id]
    assert "still pointing at an older duplicate" not in page


def test_the_flash_says_what_was_moved(dash, store, connect):
    old = _dupe(store, 30)
    _dupe(store, 1)
    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([old.id])
    store.upsert_instruction(instruction)

    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    page = _callback(dash).get_data(as_text=True)
    assert "1 instruction(s) still pointing at an older duplicate" in page


# --- one rule about which row survives ---------------------------------------------------- #

def test_the_page_the_reconnect_and_the_cleanup_agree_on_the_survivor(dash, store, connect):
    """Three places used to decide this. If they ever disagreed, one of them would
    repoint instructions at a row another was about to delete."""
    from aismm.dashboard.app import account_groups

    old = _dupe(store, 30)
    newest = _dupe(store, 1)
    rows = store.list_accounts()

    survivor = account_groups(rows)[(newest.platform, newest.external_id)][-1]
    assert survivor.id == newest.id

    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert old.id in page and newest.id in page          # both listed

    connect["identities"] = [_identity("1784141", "genaicomicbook")]
    _callback(dash)
    dash.test_client().post("/accounts/prune-duplicates", follow_redirects=True)
    assert [a.id for a in store.list_accounts()] == [newest.id]
