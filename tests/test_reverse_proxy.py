from dataclasses import replace
from importlib import import_module

from flask import Flask, url_for

from aismm import assets
from aismm.config import DashboardSettings, _path_prefix, settings
from aismm.dashboard.app import ReverseProxyPrefixMiddleware


def _prefixed_app() -> Flask:
    app = Flask(__name__)
    app.config["APPLICATION_ROOT"] = "/aismm"

    @app.route("/")
    def index():
        return url_for("runs")

    @app.route("/runs")
    def runs():
        return "runs"

    app.wsgi_app = ReverseProxyPrefixMiddleware(app.wsgi_app, "/aismm")
    return app


def test_prefix_is_normalized():
    assert _path_prefix(None) == ""
    assert _path_prefix("/") == ""
    assert _path_prefix(" aismm/ ") == "/aismm"
    assert _path_prefix("//tools//aismm//") == "/tools/aismm"


def test_public_urls_include_prefix_once():
    dashboard = DashboardSettings(
        base_url="https://example.com", reverse_proxy_prefix="/aismm")
    assert dashboard.public_base_url == "https://example.com/aismm"
    assert dashboard.external_url("/assets/example.png") == (
        "https://example.com/aismm/assets/example.png")

    already_prefixed = DashboardSettings(
        base_url="https://example.com/aismm/", reverse_proxy_prefix="/aismm")
    assert already_prefixed.public_base_url == "https://example.com/aismm"


def test_oauth_and_asset_urls_include_prefix(monkeypatch):
    dashboard = DashboardSettings(
        base_url="https://example.com", reverse_proxy_prefix="/aismm")
    prefixed_settings = replace(settings, dashboard=dashboard)
    monkeypatch.setattr(assets, "settings", prefixed_settings)

    assert prefixed_settings.redirect_uri("twitter") == (
        "https://example.com/aismm/oauth/twitter/callback")
    assert assets.public_url("/tmp/example.png") == (
        "https://example.com/aismm/assets/example.png")


def test_create_app_installs_prefix(monkeypatch):
    dashboard_app = import_module("aismm.dashboard.app")
    dashboard = replace(settings.dashboard, reverse_proxy_prefix="/aismm")
    monkeypatch.setattr(dashboard_app, "settings", replace(settings, dashboard=dashboard))
    app = dashboard_app.create_app()

    @app.route("/prefix-check")
    def prefix_check():
        return url_for("runs")

    response = app.test_client().get("/aismm/prefix-check")
    assert response.status_code == 200
    assert response.text == "/aismm/runs"


def test_routes_and_generated_urls_use_prefix_when_proxy_passes_it():
    client = _prefixed_app().test_client()
    response = client.get("/aismm/")
    assert response.status_code == 200
    assert response.text == "/aismm/runs"
    assert client.get("/aismm/runs").status_code == 200


def test_routes_and_generated_urls_use_prefix_when_proxy_strips_it():
    client = _prefixed_app().test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == "/aismm/runs"
    assert client.get("/runs").status_code == 200
