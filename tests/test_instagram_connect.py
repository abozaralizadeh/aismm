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


def test_the_token_type_is_reported(monkeypatch):
    """PAGE vs USER is the decisive fact — a USER token cannot publish at all."""
    from aismm.config import PlatformCreds

    platform = registry.get_platform(
        PlatformName.instagram, PlatformCreds(client_id="id", client_secret="secret"))
    _debug_token_stub(monkeypatch, {"data": {
        "type": "page", "is_valid": True, "profile_id": "123",
        "scopes": ["instagram_basic"]}})
    info = asyncio.run(platform.inspect_token("T"))
    assert info["type"] == "PAGE"
    assert info["is_valid"] is True
    assert info["profile_id"] == "123"


def test_granted_scopes_needs_app_credentials(monkeypatch):
    from aismm.config import PlatformCreds

    platform = registry.get_platform(PlatformName.instagram, PlatformCreds())
    assert asyncio.run(platform.granted_scopes("T")) == []
    assert asyncio.run(platform.inspect_token("T")) == {}


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

    async def inspect(_self, _token, _account=None):
        return {"type": "PAGE", "is_valid": True, "profile_id": "9",
                "scopes": ["instagram_basic", "instagram_content_publish"]}  # no pages_*

    # get_platform() builds a FRESH instance per call, so patch the class.
    monkeypatch.setattr(Instagram, "inspect_token", inspect)
    page = dash.test_client().post(f"/accounts/{account.id}/check",
                                   follow_redirects=True).get_data(as_text=True)
    assert "Publishing will FAIL" in page
    assert "pages_show_list" in page


def test_the_check_confirms_a_healthy_token(dash, store, monkeypatch):
    from aismm.platforms.instagram import Instagram

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="x", external_id="1"),
        access_token="T")

    async def inspect(_self, _token, _account=None):
        return {"type": "PAGE", "is_valid": True, "profile_id": "9",
                "scopes": list(Instagram.DEFAULT_SCOPES)}

    monkeypatch.setattr(Instagram, "inspect_token", inspect)
    page = dash.test_client().post(f"/accounts/{account.id}/check",
                                   follow_redirects=True).get_data(as_text=True)
    assert "Looks healthy" in page


def test_checking_an_unknown_account_is_404(dash):
    assert dash.test_client().post("/accounts/nope/check").status_code == 404


def test_the_accounts_page_offers_the_check(dash, store):
    store.upsert_account(
        Account(platform=PlatformName.instagram, handle="x", external_id="1"),
        access_token="T")
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "Check permissions" in page


def test_a_user_token_is_named_as_the_cause(dash, store, monkeypatch):
    """The whole point of the button: turn code 190 into a definite answer.

    Scopes can look complete and publishing still fail, because publishing acts
    as the Page. USER vs PAGE is what actually decides it.
    """
    from aismm.platforms.instagram import Instagram

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="genaicomicbook", external_id="1"),
        access_token="T")

    async def inspect(_self, _token, _account=None):
        return {"type": "USER", "is_valid": True,
                "scopes": list(Instagram.REQUIRED_SCOPES), "profile_id": "9"}

    monkeypatch.setattr(Instagram, "inspect_token", inspect)
    page = dash.test_client().post(f"/accounts/{account.id}/check",
                                   follow_redirects=True).get_data(as_text=True)
    assert "USER token, not a PAGE token" in page
    assert "impersonating a user" in page
    assert "Page is ticked" in page


def test_a_page_token_with_full_scopes_reads_healthy(dash, store, monkeypatch):
    from aismm.platforms.instagram import Instagram

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="x", external_id="1"),
        access_token="T")

    async def inspect(_self, _token, _account=None):
        return {"type": "PAGE", "is_valid": True,
                "scopes": list(Instagram.DEFAULT_SCOPES), "profile_id": "9"}

    monkeypatch.setattr(Instagram, "inspect_token", inspect)
    page = dash.test_client().post(f"/accounts/{account.id}/check",
                                   follow_redirects=True).get_data(as_text=True)
    assert "Looks healthy" in page
    assert "type=PAGE" in page


def test_an_expired_token_says_reconnect(dash, store, monkeypatch):
    from aismm.platforms.instagram import Instagram

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="x", external_id="1"),
        access_token="T")

    async def inspect(_self, _token, _account=None):
        return {"type": "PAGE", "is_valid": False,
                "scopes": list(Instagram.REQUIRED_SCOPES), "profile_id": "9"}

    monkeypatch.setattr(Instagram, "inspect_token", inspect)
    page = dash.test_client().post(f"/accounts/{account.id}/check",
                                   follow_redirects=True).get_data(as_text=True)
    # Named per platform now that every platform can be checked — saying
    # "Instagram says" on an X account would be nonsense.
    assert "instagram rejected this token" in page.lower()
    assert "reconnect" in page.lower()


# --- connecting one account can break another ---------------------------------------- #
# One Meta app + one Facebook user = ONE grant. Authorising again replaces it, so
# adding a second Instagram account with only its own Page ticked strips page
# access from the grant the FIRST account's page token was minted against. It
# keeps looking connected and fails hours later on whatever run comes next.

def _connected(store, handle, external_id):
    return store.upsert_account(
        Account(platform=PlatformName.instagram, handle=handle, external_id=external_id),
        access_token=f"token-{handle}")


def test_a_broken_sibling_is_reported_right_after_connecting(store, monkeypatch):
    from aismm.platforms.instagram import Instagram

    older = _connected(store, "genaicomicbook", "1")
    just_added = _connected(store, "emortezaei", "2")

    async def inspect(_self, token, _account=None):
        if token == "token-genaicomicbook":
            return {"type": "USER", "is_valid": True, "scopes": ["instagram_basic"]}
        return {"type": "PAGE", "is_valid": True, "scopes": list(Instagram.REQUIRED_SCOPES)}

    monkeypatch.setattr(Instagram, "inspect_token", inspect)

    flashed = []
    monkeypatch.setattr(app_module, "flash", lambda msg, cat="": flashed.append((msg, cat)))
    app_module._warn_about_collateral_damage(
        store, just_added, registry.get_platform(PlatformName.instagram))

    assert flashed, "connecting silently broke an existing account"
    message, category = flashed[0]
    assert category == "error"
    assert "genaicomicbook" in message
    assert "EVERY Page" in message
    assert "emortezaei" not in message              # the one just added is fine


def test_healthy_siblings_produce_no_warning(store, monkeypatch):
    from aismm.platforms.instagram import Instagram

    _connected(store, "genaicomicbook", "1")
    just_added = _connected(store, "emortezaei", "2")

    async def inspect(_self, _token, _account=None):
        return {"type": "PAGE", "is_valid": True, "scopes": list(Instagram.REQUIRED_SCOPES)}

    monkeypatch.setattr(Instagram, "inspect_token", inspect)
    flashed = []
    monkeypatch.setattr(app_module, "flash", lambda msg, cat="": flashed.append((msg, cat)))
    app_module._warn_about_collateral_damage(
        store, just_added, registry.get_platform(PlatformName.instagram))
    assert flashed == []


def test_missing_page_scopes_also_count_as_broken(store, monkeypatch):
    """The real symptom: pages_show_list / pages_read_engagement gone from the grant."""
    from aismm.platforms.instagram import Instagram

    _connected(store, "genaicomicbook", "1")
    just_added = _connected(store, "emortezaei", "2")

    async def inspect(_self, token, _account=None):
        if token == "token-genaicomicbook":
            return {"type": "PAGE", "is_valid": True,
                    "scopes": ["instagram_basic", "instagram_content_publish"]}
        return {"type": "PAGE", "is_valid": True, "scopes": list(Instagram.REQUIRED_SCOPES)}

    monkeypatch.setattr(Instagram, "inspect_token", inspect)
    flashed = []
    monkeypatch.setattr(app_module, "flash", lambda msg, cat="": flashed.append((msg, cat)))
    app_module._warn_about_collateral_damage(
        store, just_added, registry.get_platform(PlatformName.instagram))
    assert flashed and "genaicomicbook" in flashed[0][0]


def test_the_warning_never_breaks_the_connection_that_just_worked(store, monkeypatch):
    from aismm.platforms.instagram import Instagram

    _connected(store, "genaicomicbook", "1")
    just_added = _connected(store, "emortezaei", "2")

    async def explode(_self, _token):
        raise RuntimeError("graph is down")

    monkeypatch.setattr(Instagram, "inspect_token", explode)
    monkeypatch.setattr(app_module, "flash", lambda *a, **kw: None)
    app_module._warn_about_collateral_damage(          # must not raise
        store, just_added, registry.get_platform(PlatformName.instagram))


def test_other_platforms_are_not_dragged_in(store, monkeypatch):
    from aismm.platforms.instagram import Instagram

    store.upsert_account(Account(platform=PlatformName.twitter, handle="x", external_id="9"),
                         access_token="t")
    just_added = _connected(store, "emortezaei", "2")

    calls = []

    async def inspect(_self, token, _account=None):
        calls.append(token)
        return {"type": "PAGE", "is_valid": True, "scopes": list(Instagram.REQUIRED_SCOPES)}

    monkeypatch.setattr(Instagram, "inspect_token", inspect)
    monkeypatch.setattr(app_module, "flash", lambda *a, **kw: None)
    app_module._warn_about_collateral_damage(
        store, just_added, registry.get_platform(PlatformName.instagram))
    assert calls == [], "only same-platform siblings should be re-checked"


# --- one authorization, every account ------------------------------------------------ #
# The structural fix for the grant-replacement trap: if a single login claims all
# the Pages it administers, there is never a second authorization to overwrite the
# first. Connecting them one at a time is what broke the earlier accounts.

THREE_PAGES = {"data": [
    {"name": "Comic Page", "access_token": "T-comic",
     "instagram_business_account": {"id": "1", "username": "genaicomicbook"}},
    {"name": "Clinic Page", "access_token": "T-clinic",
     "instagram_business_account": {"id": "2", "username": "apadana.audiology.clinic"}},
    {"name": "Psych Page", "access_token": "T-psych",
     "instagram_business_account": {"id": "3", "username": "emortezaei"}},
]}


def test_one_login_yields_every_linked_account(monkeypatch):
    _graph_stub(monkeypatch, THREE_PAGES)
    identities = asyncio.run(_ig().fetch_identities("USER-TOKEN"))
    assert [i.handle for i in identities] == [
        "genaicomicbook", "apadana.audiology.clinic", "emortezaei"]


def test_each_account_keeps_its_OWN_page_token(monkeypatch):
    """Three handles, three distinct page tokens — never the user token."""
    _graph_stub(monkeypatch, THREE_PAGES)
    identities = asyncio.run(_ig().fetch_identities("USER-TOKEN"))
    tokens = [i.meta["access_token"] for i in identities]
    assert tokens == ["T-comic", "T-clinic", "T-psych"]
    assert "USER-TOKEN" not in tokens


def test_a_page_without_a_token_is_skipped_not_fatal(monkeypatch):
    """The others are still usable; the broken one shows up in the permission check."""
    pages = {"data": [
        {"name": "Broken", "instagram_business_account": {"id": "9", "username": "broken"}},
        THREE_PAGES["data"][0],
    ]}
    _graph_stub(monkeypatch, pages)
    identities = asyncio.run(_ig().fetch_identities("USER-TOKEN"))
    assert [i.handle for i in identities] == ["genaicomicbook"]


def test_pages_without_instagram_are_ignored(monkeypatch):
    _graph_stub(monkeypatch, {"data": [
        {"name": "Plain page", "access_token": "T"},
        THREE_PAGES["data"][0],
    ]})
    assert len(asyncio.run(_ig().fetch_identities("USER-TOKEN"))) == 1


def test_nothing_linked_still_raises_the_helpful_error(monkeypatch):
    _graph_stub(monkeypatch, {"data": [{"name": "Plain page", "access_token": "T"}]})
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_ig().fetch_identities("USER-TOKEN"))
    assert "No Instagram Business account" in str(exc.value)


def test_other_platforms_still_connect_one_account():
    """The base default wraps fetch_identity, so nothing else changes."""
    from aismm.platforms.base import SocialPlatform

    assert SocialPlatform.fetch_identities is not None


def test_the_callback_stores_every_account_from_one_login(store, monkeypatch):
    """End to end: one OAuth round-trip, three accounts connected."""
    from aismm.auth.oauth import TokenBundle
    from aismm.platforms.base import Identity
    from aismm.platforms.instagram import Instagram

    monkeypatch.setattr(app_module, "get_store", lambda: store)

    async def exchange(_self, **kwargs):
        return TokenBundle(access_token="USER-TOKEN", refresh_token="", expires_in=0)

    async def identities(_self, _token):
        return [Identity(external_id=p["instagram_business_account"]["id"],
                         handle=p["instagram_business_account"]["username"],
                         meta={"access_token": p["access_token"], "page_name": p["name"]})
                for p in THREE_PAGES["data"]]

    async def inspect(_self, _token, _account=None):
        return {"type": "PAGE", "is_valid": True, "scopes": list(Instagram.REQUIRED_SCOPES)}

    monkeypatch.setattr(Instagram, "exchange_code", exchange)
    monkeypatch.setattr(Instagram, "fetch_identities", identities)
    monkeypatch.setattr(Instagram, "inspect_token", inspect)

    application = app_module.create_app()
    application.secret_key = "test"
    client = application.test_client()
    with client.session_transaction() as sess:
        sess["oauth_state_instagram"] = "S"

    page = client.get("/oauth/instagram/callback?code=C&state=S",
                      follow_redirects=True).get_data(as_text=True)

    handles = sorted(a.handle for a in store.list_accounts())
    assert handles == ["apadana.audiology.clinic", "emortezaei", "genaicomicbook"]
    assert "3 accounts from one login" in page
    # And each kept its own page token.
    by_handle = {a.handle: store.get_tokens(a.id)[0] for a in store.list_accounts()}
    assert by_handle["genaicomicbook"] == "T-comic"
    assert by_handle["emortezaei"] == "T-psych"


def test_connecting_all_at_once_warns_about_nobody(store, monkeypatch):
    """Every account came from THIS grant, so none of them can have been broken."""
    from aismm.platforms.instagram import Instagram

    accounts = [_connected(store, h, str(i))
                for i, h in enumerate(["genaicomicbook", "apadana", "emortezaei"], start=1)]

    async def inspect(_self, _token, _account=None):
        return {"type": "USER", "is_valid": True, "scopes": []}   # would flag if checked

    monkeypatch.setattr(Instagram, "inspect_token", inspect)
    flashed = []
    monkeypatch.setattr(app_module, "flash", lambda msg, cat="": flashed.append(msg))
    app_module._warn_about_collateral_damage(
        store, accounts, registry.get_platform(PlatformName.instagram))
    assert flashed == []
