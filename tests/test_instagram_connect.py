"""Connecting an Instagram account, and proving the token can actually publish.

Publishing acts AS THE PAGE, so the stored token has to be the *page's* token.
Graph only hands one back when the login really granted page access. The original
code fell back to the user token when a page arrived without one — which looks
like a successful connection and then fails, minutes and one generated image
later, with:

    Any of the pages_read_engagement, pages_manage_metadata, pages_read_user_content,
    pages_manage_ads, pages_show_list or pages_messaging permission(s) must be
    granted before impersonating a user's page. [code=190]

A connection that cannot publish must be refused at connect time, where the fix
("re-tick the Page in the dialog") is still obvious.
"""
import asyncio

import httpx
import pytest

from aismm.dashboard import app as app_module
from aismm.models import Account, PlatformName
from aismm.platforms import registry


def _graph_stub(monkeypatch, payload, status=200):
    from aismm.platforms import instagram as ig

    class _Resp:
        status_code = status
        request = httpx.Request("GET", "https://graph.facebook.com/v21.0/me/accounts")

        def json(self):
            return payload

        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPStatusError("boom", request=self.request, response=self)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(ig.httpx, "AsyncClient", lambda **kw: _Client())


PAGE_WITH_IG = {
    "name": "Comic Page",
    "access_token": "PAGE-TOKEN",
    "instagram_business_account": {"id": "17841400000000000", "username": "genaicomicbook"},
}


def _ig():
    return registry.get_platform(PlatformName.instagram)


# --- the happy path ------------------------------------------------------------------ #

def test_the_page_token_is_what_gets_stored(monkeypatch):
    _graph_stub(monkeypatch, {"data": [PAGE_WITH_IG]})
    identity = asyncio.run(_ig().fetch_identity("USER-TOKEN"))
    assert identity.meta["access_token"] == "PAGE-TOKEN"
    assert identity.external_id == "17841400000000000"
    assert identity.handle == "genaicomicbook"
    assert identity.meta["page_name"] == "Comic Page"


def test_the_first_page_with_an_instagram_account_wins(monkeypatch):
    _graph_stub(monkeypatch, {"data": [
        {"name": "No IG here", "access_token": "T1"},
        PAGE_WITH_IG,
    ]})
    identity = asyncio.run(_ig().fetch_identity("USER-TOKEN"))
    assert identity.handle == "genaicomicbook"


# --- the failure this module exists for ---------------------------------------------- #

def test_a_page_without_a_token_is_refused_not_silently_downgraded(monkeypatch):
    """The user token must NEVER be stored as the page token."""
    page = {k: v for k, v in PAGE_WITH_IG.items() if k != "access_token"}
    _graph_stub(monkeypatch, {"data": [page]})

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_ig().fetch_identity("USER-TOKEN"))
    assert "page access token" in str(exc.value)
    assert "USER-TOKEN" not in str(exc.value)          # never echo a credential


def test_that_refusal_says_what_to_do_in_the_dialog(monkeypatch):
    page = {k: v for k, v in PAGE_WITH_IG.items() if k != "access_token"}
    _graph_stub(monkeypatch, {"data": [page]})
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_ig().fetch_identity("USER-TOKEN"))
    message = str(exc.value)
    assert "PAGE itself is ticked" in message
    assert "connect again" in message
    assert "Comic Page" in message                     # names the page


def test_no_linked_instagram_account_reports_how_many_pages_were_seen(monkeypatch):
    _graph_stub(monkeypatch, {"data": [{"name": "A"}, {"name": "B"}]})
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_ig().fetch_identity("USER-TOKEN"))
    assert "2 page(s) visible" in str(exc.value)


def test_no_pages_at_all(monkeypatch):
    _graph_stub(monkeypatch, {"data": []})
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_ig().fetch_identity("USER-TOKEN"))
    assert "0 page(s) visible" in str(exc.value)


# --- reading what a token really carries --------------------------------------------- #

def _debug_token_stub(monkeypatch, payload, status=200):
    from aismm.platforms import instagram as ig

    class _Resp:
        status_code = status

        def json(self):
            return payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(ig.httpx, "AsyncClient", lambda **kw: _Client())


def test_granted_scopes_are_read_from_debug_token(monkeypatch):
    from aismm.config import PlatformCreds

    platform = registry.get_platform(
        PlatformName.instagram, PlatformCreds(client_id="id", client_secret="secret"))
    _debug_token_stub(monkeypatch, {"data": {"scopes": ["instagram_basic", "pages_show_list"]}})
    assert asyncio.run(platform.granted_scopes("T")) == ["instagram_basic", "pages_show_list"]


def test_granted_scopes_needs_app_credentials(monkeypatch):
    from aismm.config import PlatformCreds

    platform = registry.get_platform(PlatformName.instagram, PlatformCreds())
    assert asyncio.run(platform.granted_scopes("T")) == []


def test_granted_scopes_never_raises(monkeypatch):
    """It is a diagnostic; it must not be able to break the accounts page."""
    from aismm.config import PlatformCreds
    from aismm.platforms import instagram as ig

    class _Boom:
        async def __aenter__(self):
            raise httpx.ConnectError("no network")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ig.httpx, "AsyncClient", lambda **kw: _Boom())
    platform = registry.get_platform(
        PlatformName.instagram, PlatformCreds(client_id="i", client_secret="s"))
    assert asyncio.run(platform.granted_scopes("T")) == []


# --- the dashboard check button ------------------------------------------------------ #

@pytest.fixture()
def dash(store, monkeypatch):
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


def test_the_check_names_the_missing_permission(dash, store, monkeypatch):
    from aismm.platforms.instagram import Instagram

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="x", external_id="1"),
        access_token="T")

    async def granted(_self, _token):
        return ["instagram_basic", "instagram_content_publish"]   # no pages_* at all

    # get_platform() builds a FRESH instance per call, so patch the class.
    monkeypatch.setattr(Instagram, "granted_scopes", granted)
    page = dash.test_client().post(f"/accounts/{account.id}/check",
                                   follow_redirects=True).get_data(as_text=True)
    assert "MISSING" in page
    assert "pages_show_list" in page


def test_the_check_confirms_a_healthy_token(dash, store, monkeypatch):
    from aismm.platforms.instagram import Instagram

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="x", external_id="1"),
        access_token="T")

    async def granted(_self, _token):
        return list(Instagram.REQUIRED_SCOPES)

    monkeypatch.setattr(Instagram, "granted_scopes", granted)
    page = dash.test_client().post(f"/accounts/{account.id}/check",
                                   follow_redirects=True).get_data(as_text=True)
    assert "every permission publishing needs" in page


def test_checking_an_unknown_account_is_404(dash):
    assert dash.test_client().post("/accounts/nope/check").status_code == 404


def test_the_accounts_page_offers_the_check(dash, store):
    store.upsert_account(
        Account(platform=PlatformName.instagram, handle="x", external_id="1"),
        access_token="T")
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "Check permissions" in page
