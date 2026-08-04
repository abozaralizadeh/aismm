"""Dashboard SSO: the guard, the allowlist, and ID token validation.

No network and no real identity provider: discovery and the token endpoint are
stubbed, so these exercise our own logic only.
"""
import base64
import dataclasses
import json
import time

import pytest

from aismm import config as config_module
from aismm.config import AuthSettings
from aismm.dashboard import app as app_module
from aismm.dashboard import sso
from aismm.store.local_store import LocalStore

ISSUER = "https://id.example.com"
CLIENT_ID = "client-abc"

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "userinfo_endpoint": f"{ISSUER}/userinfo",
}


def _id_token(**claims) -> str:
    payload = {"iss": ISSUER, "aud": CLIENT_ID, "exp": time.time() + 600, **claims}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


@pytest.fixture()
def auth_app(monkeypatch, tmp_path):
    """A dashboard app with SSO enabled, a one-address allowlist, and a throwaway store."""
    def build(**overrides):
        auth = AuthSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret",
                            allowed_emails=["me@example.com"], **overrides)
        patched = dataclasses.replace(config_module.settings, auth=auth, data_dir=tmp_path)
        for module in (sso, app_module, config_module):
            monkeypatch.setattr(module, "settings", patched)
        monkeypatch.setattr(sso, "discovery", lambda *a, **kw: DISCOVERY)
        # Never touch the developer's real SQLite file.
        store = LocalStore(db_url=f"sqlite:///{tmp_path/'test.sqlite'}")
        monkeypatch.setattr(app_module, "get_store", lambda: store)
        app = app_module.create_app()
        app.secret_key = "test-key"
        return app
    return build


# --- the guard ---------------------------------------------------------------- #

def test_dashboard_redirects_to_login_when_signed_out(auth_app):
    client = auth_app().test_client()
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_every_dashboard_route_is_guarded(auth_app):
    client = auth_app().test_client()
    for path in ("/", "/accounts", "/instructions", "/instructions/new", "/runs", "/settings"):
        assert client.get(path).status_code == 302, f"{path} was reachable while signed out"


def test_assets_stay_public_for_instagram(auth_app, tmp_path):
    """Instagram fetches media server-side with no cookie — guarding this breaks publishing."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "clip.mp4").write_bytes(b"video-bytes")
    client = auth_app().test_client()
    resp = client.get("/assets/clip.mp4")
    assert resp.status_code == 200
    assert resp.data == b"video-bytes"


def test_healthz_is_public(auth_app):
    assert auth_app().test_client().get("/healthz").status_code == 200


def test_login_page_renders_without_a_session(auth_app):
    resp = auth_app().test_client().get("/login")
    assert resp.status_code == 200
    assert b"Sign in with" in resp.data


def test_dashboard_is_open_when_sso_is_disabled(monkeypatch, tmp_path):
    patched = dataclasses.replace(config_module.settings,
                                  auth=AuthSettings(), data_dir=tmp_path)
    for module in (sso, app_module, config_module):
        monkeypatch.setattr(module, "settings", patched)
    app = app_module.create_app()
    # No guard installed at all -> no /login route exists.
    assert app.test_client().get("/healthz").status_code == 200
    assert app.test_client().get("/login").status_code == 404


# --- allowlist ---------------------------------------------------------------- #

@pytest.mark.parametrize("email,allowed", [
    ("me@example.com", True),
    ("ME@Example.com", True),      # case-insensitive
    ("someone@example.com", False),
    ("", False),
])
def test_email_allowlist(email, allowed):
    auth = AuthSettings(allowed_emails=["me@example.com"])
    assert auth.allows(email) is allowed


@pytest.mark.parametrize("email,allowed", [
    ("anyone@corp.com", True),
    ("anyone@sub.corp.com", False),   # exact domain only
    ("anyone@other.com", False),
])
def test_domain_allowlist(email, allowed):
    assert AuthSettings(allowed_domains=["corp.com"]).allows(email) is allowed


def test_no_allowlist_denies_everyone():
    """Fail closed: an empty allowlist must not mean 'any account at the provider'."""
    auth = AuthSettings(issuer=ISSUER, client_id="x", client_secret="y")
    assert auth.has_allowlist is False
    assert auth.allows("anyone@gmail.com") is False


def test_sso_enabled_follows_configuration_and_override():
    assert AuthSettings().enabled is False
    configured = AuthSettings(issuer=ISSUER, client_id="x", client_secret="y")
    assert configured.enabled is True
    assert dataclasses.replace(configured, enabled_override=False).enabled is False


# --- ID token validation ------------------------------------------------------ #

def _validate(monkeypatch, token, *, nonce="n", issuer=ISSUER):
    patched = dataclasses.replace(
        config_module.settings,
        auth=AuthSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s"))
    monkeypatch.setattr(sso, "settings", patched)
    sso.validate_claims(sso.decode_claims(token), expected_nonce=nonce, issuer=issuer)


def test_valid_claims_pass(monkeypatch):
    _validate(monkeypatch, _id_token(nonce="n"))


def test_expired_token_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="expired"):
        _validate(monkeypatch, _id_token(nonce="n", exp=time.time() - 3600))


def test_wrong_audience_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="audience"):
        _validate(monkeypatch, _id_token(nonce="n", aud="someone-elses-app"))


def test_wrong_issuer_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="issuer"):
        _validate(monkeypatch, _id_token(nonce="n", iss="https://evil.example.com"))


def test_nonce_mismatch_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="nonce"):
        _validate(monkeypatch, _id_token(nonce="other"))


def test_entra_templated_issuer_resolves_via_tid(monkeypatch):
    """Entra's multi-tenant discovery advertises {tenantid}; the token carries `tid`."""
    tenant = "11111111-2222-3333-4444-555555555555"
    token = _id_token(nonce="n", iss=f"https://login.microsoftonline.com/{tenant}/v2.0", tid=tenant)
    _validate(monkeypatch, token, issuer="https://login.microsoftonline.com/{tenantid}/v2.0")


def test_audience_list_is_accepted(monkeypatch):
    _validate(monkeypatch, _id_token(nonce="n", aud=["other", CLIENT_ID]))


def test_malformed_token_is_rejected():
    with pytest.raises(ValueError, match="Malformed"):
        sso.decode_claims("not-a-jwt")


# --- callback ----------------------------------------------------------------- #

def test_callback_rejects_state_mismatch(auth_app):
    client = auth_app().test_client()
    resp = client.get("/auth/callback?code=x&state=forged")
    assert resp.status_code == 403
    assert b"state mismatch" in resp.data


def test_callback_reports_provider_error(auth_app):
    resp = auth_app().test_client().get("/auth/callback?error=access_denied")
    assert resp.status_code == 403


def _login(client, monkeypatch, email, name="Test User", path="/auth/callback"):
    """Drive /login then /auth/callback with a stubbed token endpoint."""
    with client.session_transaction() as sess:
        sess[sso._STATE_KEY] = "st"
        sess[sso._NONCE_KEY] = "no"

    class _Resp:
        def json(self):
            return {"id_token": _id_token(nonce="no", email=email, name=name),
                    "access_token": "at"}

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(sso.httpx, "Client", _Client)
    return client.get(f"{path}?code=abc&state=st")


def test_allowed_identity_gets_a_session(auth_app, monkeypatch):
    client = auth_app().test_client()
    resp = _login(client, monkeypatch, "me@example.com")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess[sso._SESSION_USER]["email"] == "me@example.com"
    # ...and the dashboard is now reachable.
    assert client.get("/runs").status_code == 200


def test_unlisted_identity_is_refused(auth_app, monkeypatch):
    client = auth_app().test_client()
    resp = _login(client, monkeypatch, "stranger@evil.com")
    assert resp.status_code == 403
    assert b"not authorized" in resp.data
    with client.session_transaction() as sess:
        assert sso._SESSION_USER not in sess


def test_logout_clears_the_session(auth_app, monkeypatch):
    client = auth_app().test_client()
    _login(client, monkeypatch, "me@example.com")
    client.get("/logout")
    with client.session_transaction() as sess:
        assert sso._SESSION_USER not in sess
    assert client.get("/").status_code == 302


def test_login_redirect_target_cannot_leave_the_app(auth_app, monkeypatch):
    """The post-login redirect must not be usable as an open redirect."""
    client = auth_app().test_client()
    with client.session_transaction() as sess:
        sess[sso._NEXT_KEY] = "//evil.example.com/steal"
    resp = _login(client, monkeypatch, "me@example.com")
    assert "evil.example.com" not in resp.headers["Location"]


# --- behind a reverse-proxy prefix --------------------------------------------- #
# The dashboard is commonly mounted at /aismm. Flask strips SCRIPT_NAME before
# routing, so `request.full_path` is the UNPREFIXED path — remembering it
# verbatim sent every sign-in to /instructions instead of /aismm/instructions.

@pytest.fixture()
def prefixed_app(monkeypatch, tmp_path):
    def build(prefix="/aismm"):
        auth = AuthSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s3cret",
                            allowed_emails=["me@example.com"])
        dash = dataclasses.replace(config_module.settings.dashboard,
                                   reverse_proxy_prefix=prefix)
        patched = dataclasses.replace(config_module.settings, auth=auth,
                                      dashboard=dash, data_dir=tmp_path)
        for module in (sso, app_module, config_module):
            monkeypatch.setattr(module, "settings", patched)
        monkeypatch.setattr(sso, "discovery", lambda *a, **kw: DISCOVERY)
        store = LocalStore(db_url=f"sqlite:///{tmp_path/'prefixed.sqlite'}")
        monkeypatch.setattr(app_module, "get_store", lambda: store)
        app = app_module.create_app()
        app.secret_key = "test-key"
        return app
    return build


def test_the_login_redirect_keeps_the_prefix(prefixed_app):
    resp = prefixed_app().test_client().get("/aismm/instructions")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/aismm/login")


def test_the_remembered_destination_keeps_the_prefix(prefixed_app, monkeypatch):
    """The reported bug: signed in, then dropped outside the mounted app."""
    client = prefixed_app().test_client()
    client.get("/aismm/instructions")            # bounces to login, remembers where
    resp = _login(client, monkeypatch, "me@example.com", path="/aismm/auth/callback")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/aismm/instructions")


def test_the_default_destination_keeps_the_prefix(prefixed_app, monkeypatch):
    client = prefixed_app().test_client()
    resp = _login(client, monkeypatch, "me@example.com", path="/aismm/auth/callback")
    location = resp.headers["Location"]
    assert location.rstrip("/").endswith("/aismm")


def test_a_prefix_is_not_applied_twice(prefixed_app, monkeypatch):
    """nginx may pass the prefix through rather than stripping it."""
    client = prefixed_app().test_client()
    with client.session_transaction() as sess:
        sess[sso._NEXT_KEY] = "/aismm/runs"
    resp = _login(client, monkeypatch, "me@example.com", path="/aismm/auth/callback")
    assert "/aismm/aismm" not in resp.headers["Location"]
    assert resp.headers["Location"].endswith("/aismm/runs")


# --- ...and with NO prefix, which is the default --------------------------------- #
# An empty REVERSE_PROXY_PREFIX means "no reverse proxy": nothing may gain a
# prefix it never had. The fix for the prefixed case reads request.script_root,
# which is "" here — these pin that it stays a no-op.

def test_an_empty_prefix_means_no_prefix_anywhere(auth_app):
    """None, "", whitespace and "/" all normalize to no prefix at all."""
    from aismm.config import _path_prefix

    assert [_path_prefix(v) for v in (None, "", "   ", "/")] == ["", "", "", ""]


def test_without_a_prefix_the_app_is_not_wrapped(auth_app):
    app = auth_app()
    assert app.config.get("APPLICATION_ROOT") in (None, "/")
    assert not isinstance(app.wsgi_app, app_module.ReverseProxyPrefixMiddleware)


def test_the_remembered_destination_is_untouched_without_a_prefix(auth_app, monkeypatch):
    client = auth_app().test_client()
    assert client.get("/instructions").headers["Location"].endswith("/login")
    resp = _login(client, monkeypatch, "me@example.com")
    assert resp.headers["Location"].endswith("/instructions")
    assert "//" not in resp.headers["Location"].split("://", 1)[-1]


def test_the_default_destination_is_the_bare_root_without_a_prefix(auth_app, monkeypatch):
    client = auth_app().test_client()
    resp = _login(client, monkeypatch, "me@example.com")
    location = resp.headers["Location"]
    assert location.endswith("/")
    assert location.rstrip("/").rsplit("://", 1)[-1].count("/") == 0   # no path segment


def test_safe_next_is_a_no_op_without_a_prefix(auth_app):
    app = auth_app()
    with app.test_request_context("/instructions"):
        assert sso._safe_next("/instructions") == "/instructions"
        assert sso._safe_next("/runs?status=failed") == "/runs?status=failed"


def test_safe_next_still_refuses_an_offsite_target_without_a_prefix(auth_app):
    app = auth_app()
    with app.test_request_context("/"):
        assert sso._safe_next("//evil.example/") == "/"
        assert sso._safe_next("https://evil.example/") == "/"


def test_a_proxy_that_mounts_the_app_itself_is_still_honoured(auth_app):
    """SCRIPT_NAME set by the proxy, REVERSE_PROXY_PREFIX unset: follow the
    request, not the config — this is what url_for does too."""
    app = auth_app()
    with app.test_request_context("/instructions",
                                  environ_overrides={"SCRIPT_NAME": "/mounted"}):
        from flask import url_for

        assert sso._safe_next("/instructions") == "/mounted/instructions"
        assert sso._safe_next("/instructions").startswith(url_for("index"))
