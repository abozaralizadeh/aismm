"""The X destination on a connection: which community, and who else sees it.

A community post is visible only inside that community. X's own composer puts an
"Also share with followers" switch beside the community picker, and without the
equivalent here a post aimed at growing an audience reaches a room instead.
"""
import dataclasses

import pytest

from aismm import config as config_module
from aismm.config import AuthSettings
from aismm.dashboard import app as app_module
from aismm.dashboard import sso
from aismm.models import Account, PlatformName


@pytest.fixture()
def dash(store, monkeypatch, tmp_path):
    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


@pytest.fixture()
def account(store):
    return store.upsert_account(
        Account(platform=PlatformName.twitter, handle="abo0zar", external_id="9"),
        access_token="t")


def _save(dash, account, **form):
    return dash.test_client().post(f"/accounts/{account.id}/community", data=form,
                                   follow_redirects=True)


def test_the_checkbox_is_on_the_connection(dash, account):
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert 'name="share_with_followers"' in page
    assert "Also share with followers" in page


def test_it_is_saved_with_the_community(dash, store, account):
    _save(dash, account, community_id="123", share_with_followers="on")
    saved = store.get_account(account.id)
    assert saved.meta["community_id"] == "123"
    assert saved.meta["share_with_followers"] is True


def test_it_can_be_turned_off(dash, store, account):
    _save(dash, account, community_id="123", share_with_followers="on")
    _save(dash, account, community_id="123")          # unticked = absent from the form
    assert store.get_account(account.id).meta["share_with_followers"] is False


def test_it_shows_as_ticked_once_saved(dash, store, account):
    _save(dash, account, community_id="123", share_with_followers="on")
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "checked" in page


def test_clearing_the_community_clears_the_flag_too(dash, store, account):
    """Otherwise it silently applies to whichever community is set next."""
    _save(dash, account, community_id="123", share_with_followers="on")
    _save(dash, account, community_id="")
    meta = store.get_account(account.id).meta
    assert "community_id" not in meta
    assert "share_with_followers" not in meta


def test_the_confirmation_says_who_will_see_the_posts(dash, account):
    page = _save(dash, account, community_id="123").get_data(as_text=True)
    assert "followers will not see them" in page
    page = _save(dash, account, community_id="123",
                 share_with_followers="on").get_data(as_text=True)
    assert "and to your followers" in page


def test_a_non_numeric_community_is_refused(dash, store, account):
    page = _save(dash, account, community_id="not-an-id").get_data(as_text=True)
    assert "digits only" in page
    assert "community_id" not in store.get_account(account.id).meta


def test_the_form_is_only_shown_for_x(dash, store):
    store.upsert_account(Account(platform=PlatformName.instagram, handle="ig",
                                 external_id="1"), access_token="t")
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert page.count('name="share_with_followers"') == 0


def test_it_cannot_be_set_on_another_platforms_account(dash, store):
    other = store.upsert_account(Account(platform=PlatformName.instagram, external_id="1"),
                                 access_token="t")
    assert dash.test_client().post(f"/accounts/{other.id}/community",
                                   data={"community_id": "123"}).status_code == 404
