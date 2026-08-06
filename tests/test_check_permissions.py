"""The "Check permissions" button, on every platform.

Reported: it worked on Instagram and answered
``'Twitter' object has no attribute 'inspect_token'`` everywhere else. Instagram
had a real implementation (Graph ``/debug_token``) and nothing else had any, so
the button was a diagnostic that broke on three of the four platforms it was
offered for.

Only Instagram and Google expose token introspection. For the rest the honest
check is to USE the token — the cheapest authenticated call there is — and report
the scopes recorded when the account was connected.
"""
import asyncio
import dataclasses

import pytest

from aismm import config as config_module
from aismm.config import AuthSettings
from aismm.dashboard import app as app_module
from aismm.dashboard import sso
from aismm.models import Account, PlatformName
from aismm.platforms.base import Identity
from aismm.platforms.registry import get_platform


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


def _account(store, platform, **meta):
    account = Account(platform=platform, handle="tester", external_id="9")
    if meta:
        account.set_meta(meta)
    return store.upsert_account(account, access_token="T")


def _check(dash, account):
    return dash.test_client().post(f"/accounts/{account.id}/check",
                                   follow_redirects=True).get_data(as_text=True)


# --- every platform answers ---------------------------------------------------------- #

@pytest.mark.parametrize("platform", list(PlatformName))
def test_every_platform_can_be_inspected(platform):
    """The AttributeError was the whole bug: the method simply did not exist."""
    assert hasattr(get_platform(platform), "inspect_token")


@pytest.mark.parametrize("platform", [PlatformName.twitter, PlatformName.tiktok,
                                      PlatformName.youtube])
def test_a_working_token_is_reported_as_working(platform, monkeypatch):
    integration = get_platform(platform)

    async def identity(_token):
        return Identity(external_id="9", handle="tester")

    monkeypatch.setattr(integration, "fetch_identity", identity)
    info = asyncio.run(integration.inspect_token("T"))
    assert info["is_valid"] is True
    assert info["handle"] == "tester"


def test_a_rejected_token_is_reported_with_the_reason(monkeypatch):
    integration = get_platform(PlatformName.twitter)

    async def identity(_token):
        raise RuntimeError("X API 401: Unauthorized")

    monkeypatch.setattr(integration, "fetch_identity", identity)
    info = asyncio.run(integration.inspect_token("T"))
    assert info["is_valid"] is False
    assert "401" in info["error"]


def test_the_scopes_recorded_at_connect_are_reported(store, monkeypatch):
    """Most platforms cannot be asked afterwards, so the connect response is the
    only record of what was actually granted."""
    integration = get_platform(PlatformName.twitter)

    async def identity(_token):
        return Identity(external_id="9", handle="tester")

    monkeypatch.setattr(integration, "fetch_identity", identity)
    account = _account(store, PlatformName.twitter,
                       granted_scopes=["tweet.read", "tweet.write"])
    info = asyncio.run(integration.inspect_token("T", account))
    assert info["scopes"] == ["tweet.read", "tweet.write"]


def test_inspection_never_raises(monkeypatch):
    """It is a diagnostic; one that cannot answer must not break the page too."""
    integration = get_platform(PlatformName.tiktok)

    async def identity(_token):
        raise ConnectionError("network gone")

    monkeypatch.setattr(integration, "fetch_identity", identity)
    assert asyncio.run(integration.inspect_token("T"))["is_valid"] is False


# --- through the dashboard ----------------------------------------------------------- #

def test_checking_an_x_account_no_longer_errors(dash, store, monkeypatch):
    """The reported message was "'Twitter' object has no attribute inspect_token"."""
    from aismm.platforms.twitter import Twitter

    async def identity(_self, _token):
        return Identity(external_id="9", handle="abo0zar")

    monkeypatch.setattr(Twitter, "fetch_identity", identity)
    page = _check(dash, _account(store, PlatformName.twitter))
    assert "has no attribute" not in page
    assert "Could not inspect" not in page
    assert "The token works" in page


def test_a_missing_scope_is_named_on_x(dash, store, monkeypatch):
    from aismm.platforms.twitter import Twitter

    async def identity(_self, _token):
        return Identity(external_id="9", handle="abo0zar")

    monkeypatch.setattr(Twitter, "fetch_identity", identity)
    account = _account(store, PlatformName.twitter,
                       granted_scopes=["tweet.read", "users.read"])   # no tweet.write
    page = _check(dash, account)
    assert "MISSING" in page
    assert "tweet.write" in page


def test_a_full_scope_set_reads_healthy_on_x(dash, store, monkeypatch):
    from aismm.platforms.twitter import Twitter

    async def identity(_self, _token):
        return Identity(external_id="9", handle="abo0zar")

    monkeypatch.setattr(Twitter, "fetch_identity", identity)
    account = _account(store, PlatformName.twitter,
                       granted_scopes=list(Twitter.scopes))
    assert "everything publishing needs is granted" in _check(dash, account)


def test_a_rejected_x_token_says_reconnect_and_names_x(dash, store, monkeypatch):
    """Not "Instagram says…", which is what it used to say for every platform."""
    from aismm.platforms.twitter import Twitter

    async def identity(_self, _token):
        raise RuntimeError("X API 401: Unauthorized")

    monkeypatch.setattr(Twitter, "fetch_identity", identity)
    page = _check(dash, _account(store, PlatformName.twitter))
    assert "twitter rejected this token" in page.lower()
    assert "Instagram" not in page.split("rejected")[0][-200:]


def test_an_account_with_no_recorded_scopes_is_not_called_healthy(dash, store, monkeypatch):
    """Connected before scopes were recorded: the token works, but claiming
    everything is granted would be inventing a fact."""
    from aismm.platforms.twitter import Twitter

    async def identity(_self, _token):
        return Identity(external_id="9", handle="abo0zar")

    monkeypatch.setattr(Twitter, "fetch_identity", identity)
    page = _check(dash, _account(store, PlatformName.twitter))
    assert "No scope list is available" in page
    assert "everything publishing needs is granted" not in page


def test_the_page_advice_is_only_given_for_instagram(dash, store, monkeypatch):
    """"Tick this account's Page in the dialog" means nothing on X."""
    from aismm.platforms.twitter import Twitter

    async def identity(_self, _token):
        return Identity(external_id="9", handle="abo0zar")

    monkeypatch.setattr(Twitter, "fetch_identity", identity)
    account = _account(store, PlatformName.twitter, granted_scopes=["tweet.read"])
    page = _check(dash, account)
    assert "ticking this account's Page" not in page
    assert "Disconnect and reconnect to grant them" in page
