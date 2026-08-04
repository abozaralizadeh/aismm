"""Dashboard-managed OAuth apps: several per platform, with .env as fallback.

The limitation this removes: credentials lived only in ``.env``, so a deployment
could hold exactly one app per platform and changing it meant editing a file and
restarting.
"""
import dataclasses

import pytest

from aismm import config as config_module
from aismm.config import PlatformCreds
from aismm.dashboard import app as app_module
from aismm.models import PlatformApp, PlatformName
from aismm.platforms import apps as platform_apps
from aismm.platforms.registry import get_platform
from aismm.platforms.setup_guides import GUIDES, guide_for


@pytest.fixture()
def dash(store, monkeypatch):
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


def _add(store, platform=PlatformName.instagram, name="Brand A", client_id="cid-1",
         secret="shh", enabled=True):
    app = PlatformApp(platform=platform, name=name, client_id=client_id, enabled=enabled)
    return store.upsert_platform_app(app, client_secret=secret)


# --- storage -------------------------------------------------------------------- #

def test_app_round_trip_with_encrypted_secret(store):
    app = _add(store)
    loaded = store.get_platform_app(app.id)
    assert loaded.client_id == "cid-1"
    assert store.get_app_secret(app.id) == "shh"
    assert "shh" not in loaded.client_secret_enc      # encrypted at rest


def test_several_apps_per_platform(store):
    _add(store, name="Brand A", client_id="a")
    _add(store, name="Brand B", client_id="b")
    apps = store.list_platform_apps(PlatformName.instagram)
    assert [a.client_id for a in apps] == ["a", "b"]


def test_apps_are_listed_per_platform(store):
    _add(store, platform=PlatformName.instagram)
    _add(store, platform=PlatformName.tiktok, client_id="tt")
    assert len(store.list_platform_apps(PlatformName.instagram)) == 1
    assert len(store.list_platform_apps(PlatformName.tiktok)) == 1
    assert len(store.list_platform_apps()) == 2


def test_saving_without_a_secret_keeps_the_stored_one(store):
    """The form never shows the secret back, so a blank box must not wipe it."""
    app = _add(store, secret="original")
    app.name = "renamed"
    store.upsert_platform_app(app, client_secret=None)
    assert store.get_app_secret(app.id) == "original"
    assert store.get_platform_app(app.id).name == "renamed"


def test_delete_app(store):
    app = _add(store)
    store.delete_platform_app(app.id)
    assert store.get_platform_app(app.id) is None


# --- credential resolution -------------------------------------------------------- #

def test_env_is_used_when_no_app_exists(store, monkeypatch):
    monkeypatch.setattr(platform_apps, "settings", dataclasses.replace(
        config_module.settings,
        platform_creds={"instagram": PlatformCreds(client_id="env-id", client_secret="env-s")}))
    creds = platform_apps.resolve_creds(PlatformName.instagram, store)
    assert creds.client_id == "env-id"


def test_env_stays_the_default_when_apps_also_exist(store, monkeypatch):
    """An account connected through .env must keep resolving to .env."""
    monkeypatch.setattr(platform_apps, "settings", dataclasses.replace(
        config_module.settings,
        platform_creds={"instagram": PlatformCreds(client_id="env-id", client_secret="env-s")}))
    _add(store, client_id="db-id")
    assert platform_apps.resolve_creds(PlatformName.instagram, store).client_id == "env-id"


def test_env_can_be_requested_explicitly(store, monkeypatch):
    monkeypatch.setattr(platform_apps, "settings", dataclasses.replace(
        config_module.settings,
        platform_creds={"instagram": PlatformCreds(client_id="env-id", client_secret="env-s")}))
    _add(store, client_id="db-id")
    creds = platform_apps.resolve_creds(PlatformName.instagram, store,
                                        platform_apps.ENV_APP_ID)
    assert creds.client_id == "env-id"


def test_env_cannot_be_requested_by_a_non_admin_workspace(store, monkeypatch):
    monkeypatch.setattr(platform_apps, "settings", dataclasses.replace(
        config_module.settings,
        platform_creds={"instagram": PlatformCreds(client_id="env-id", client_secret="env-s")}))
    assert not platform_apps.connection_options(PlatformName.instagram, store, "other",
                                                allow_env=False)[0]["configured"]
    assert platform_apps.resolve_creds(PlatformName.instagram, store,
                                       platform_apps.ENV_APP_ID, "other",
                                       allow_env=False).configured is False


def test_a_dashboard_app_is_used_when_env_is_empty(store):
    _add(store, client_id="db-id")
    assert platform_apps.resolve_creds(PlatformName.instagram, store).client_id == "db-id"


def test_an_explicit_app_id_selects_that_app(store):
    _add(store, name="A", client_id="a")
    second = _add(store, name="B", client_id="b", secret="b-secret")
    creds = platform_apps.resolve_creds(PlatformName.instagram, store, second.id)
    assert (creds.client_id, creds.client_secret) == ("b", "b-secret")


def test_an_app_id_from_another_platform_is_ignored(store):
    tiktok_app = _add(store, platform=PlatformName.tiktok, client_id="tt")
    _add(store, client_id="ig")
    creds = platform_apps.resolve_creds(PlatformName.instagram, store, tiktok_app.id)
    assert creds.client_id == "ig"          # falls back, never crosses platforms


def test_disabled_apps_are_not_offered(store):
    _add(store, name="off", client_id="off", enabled=False)
    assert platform_apps.available_apps(PlatformName.instagram, store) == []


def test_connection_options_lists_every_enabled_app(store):
    _add(store, name="Brand A", client_id="a")
    _add(store, name="Brand B", client_id="b")
    options = platform_apps.connection_options(PlatformName.instagram, store)
    assert [o["label"] for o in options] == ["Brand A", "Brand B"]
    assert all(o["configured"] for o in options)


def test_env_and_apps_are_offered_together(store, monkeypatch):
    """Both routes must stay reachable — hiding .env stranded its accounts."""
    monkeypatch.setattr(platform_apps, "settings", dataclasses.replace(
        config_module.settings,
        platform_creds={"instagram": PlatformCreds(client_id="env-id", client_secret="s")}))
    _add(store, name="Brand B", client_id="b")
    options = platform_apps.connection_options(PlatformName.instagram, store)
    assert [o["app_id"] for o in options] == [platform_apps.ENV_APP_ID, options[1]["app_id"]]
    assert options[0]["is_env"] is True and options[1]["label"] == "Brand B"
    assert all(o["configured"] for o in options)


def test_env_alone_is_offered_when_no_apps_exist(store, monkeypatch):
    monkeypatch.setattr(platform_apps, "settings", dataclasses.replace(
        config_module.settings,
        platform_creds={"twitter": PlatformCreds(client_id="e", client_secret="s")}))
    options = platform_apps.connection_options(PlatformName.twitter, store)
    assert len(options) == 1
    assert options[0]["app_id"] == platform_apps.ENV_APP_ID and options[0]["configured"]


def test_nothing_configured_reports_an_unconfigured_env_option(store):
    options = platform_apps.connection_options(PlatformName.youtube, store)
    assert len(options) == 1 and options[0]["configured"] is False
    assert platform_apps.is_configured(PlatformName.youtube, store) is False


def test_extra_fields_survive(store):
    app = PlatformApp(platform=PlatformName.twitter, client_id="x")
    app.set_extra({"api_key": "k", "api_secret": "s"})
    store.upsert_platform_app(app, client_secret="secret")
    creds = platform_apps.resolve_creds(PlatformName.twitter, store, app.id)
    assert creds.extra == {"api_key": "k", "api_secret": "s"}


def test_get_platform_accepts_injected_creds():
    integ = get_platform(PlatformName.instagram, PlatformCreds(client_id="c", client_secret="s"))
    assert integ.creds.client_id == "c"


# --- dashboard ------------------------------------------------------------------- #

def test_apps_page_renders_the_guide_and_redirect_uri(dash, store):
    page = dash.test_client().get("/apps/instagram").get_data(as_text=True)
    assert "Meta for Developers" in page
    assert "/oauth/instagram/callback" in page          # what to register
    assert "Instagram app ID" in page                   # the gotcha is called out


def test_apps_page_never_echoes_a_secret(dash, store):
    _add(store, secret="super-secret-value")
    page = dash.test_client().get("/apps/instagram").get_data(as_text=True)
    assert "super-secret-value" not in page


def test_creating_an_app_from_the_form(dash, store):
    dash.test_client().post("/apps", data={
        "platform": "tiktok", "name": "Client B", "client_id": "client-key",
        "client_secret": "client-secret", "enabled": "on"})
    apps = store.list_platform_apps(PlatformName.tiktok)
    assert len(apps) == 1 and apps[0].name == "Client B"
    assert store.get_app_secret(apps[0].id) == "client-secret"


def test_editing_without_a_new_secret_keeps_it(dash, store):
    app = _add(store, secret="keep-me")
    dash.test_client().post("/apps", data={
        "id": app.id, "platform": "instagram", "name": "Renamed",
        "client_id": "cid-1", "client_secret": "", "enabled": "on"})
    assert store.get_app_secret(app.id) == "keep-me"
    assert store.get_platform_app(app.id).name == "Renamed"


def test_deleting_an_app_from_the_form(dash, store):
    app = _add(store)
    dash.test_client().post(f"/apps/{app.id}/delete")
    assert store.get_platform_app(app.id) is None


def test_connect_offers_one_link_per_app(dash, store):
    _add(store, name="Brand A", client_id="a")
    _add(store, name="Brand B", client_id="b")
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "Brand A" in page and "Brand B" in page


def test_connect_without_credentials_points_at_the_apps_page(dash, store, monkeypatch):
    monkeypatch.setattr(platform_apps, "settings", dataclasses.replace(
        config_module.settings, platform_creds={}))
    response = dash.test_client().get("/oauth/instagram/start")
    assert response.status_code == 302
    assert "/apps/instagram" in response.headers["Location"]


def test_oauth_start_remembers_which_app_was_chosen(dash, store):
    app = _add(store, client_id="chosen")
    client = dash.test_client()
    client.get(f"/oauth/instagram/start?app={app.id}")
    with client.session_transaction() as session:
        assert session["oauth_app_instagram"] == app.id


# --- guides ----------------------------------------------------------------------- #

@pytest.mark.parametrize("platform", list(PlatformName))
def test_every_platform_has_a_guide(platform):
    guide = guide_for(platform)
    assert guide["steps"] and guide["id_label"] and guide["secret_label"]


def test_guides_link_to_the_right_consoles():
    assert "developers.facebook.com" in GUIDES["instagram"]["console"]
    assert "developer.x.com" in GUIDES["twitter"]["console"]
    assert "console.cloud.google.com" in GUIDES["youtube"]["console"]
    assert "developers.tiktok.com" in GUIDES["tiktok"]["console"]


def test_unknown_platform_gets_a_placeholder_guide():
    class Fake:
        value = "mastodon"

    guide = guide_for(Fake())
    assert guide["steps"]


# --- .env and dashboard apps coexist in the UI ------------------------------------- #

def _with_env(monkeypatch, **creds):
    monkeypatch.setattr(platform_apps, "settings", dataclasses.replace(
        config_module.settings, platform_creds=creds))


def test_accounts_page_offers_env_and_apps_side_by_side(dash, store, monkeypatch):
    """The reported bug: adding an app made the .env connect button vanish."""
    _with_env(monkeypatch, instagram=PlatformCreds(client_id="env-id", client_secret="s"))
    _add(store, name="Brand B", client_id="b")
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "from .env (default)" in page
    assert "Brand B" in page
    assert f"app={platform_apps.ENV_APP_ID}" in page


def test_apps_page_shows_the_env_credentials_with_a_connect_link(dash, store, monkeypatch):
    _with_env(monkeypatch, instagram=PlatformCreds(client_id="env-id-123", client_secret="s"))
    page = dash.test_client().get("/apps/instagram").get_data(as_text=True)
    assert "env-id-1" in page                       # truncated client id, never the secret
    assert "s3cret" not in page
    assert f"app={platform_apps.ENV_APP_ID}" in page


def test_connecting_with_env_records_that_choice(dash, store, monkeypatch):
    _with_env(monkeypatch, instagram=PlatformCreds(client_id="env-id", client_secret="s"))
    _add(store, name="Brand B", client_id="b")
    client = dash.test_client()
    client.get(f"/oauth/instagram/start?app={platform_apps.ENV_APP_ID}")
    with client.session_transaction() as session:
        assert session["oauth_app_instagram"] == platform_apps.ENV_APP_ID


def test_connecting_with_an_app_still_uses_that_app(dash, store, monkeypatch):
    _with_env(monkeypatch, instagram=PlatformCreds(client_id="env-id", client_secret="s"))
    app = _add(store, name="Brand B", client_id="b")
    client = dash.test_client()
    client.get(f"/oauth/instagram/start?app={app.id}")
    with client.session_transaction() as session:
        assert session["oauth_app_instagram"] == app.id
