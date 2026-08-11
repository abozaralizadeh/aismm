"""Single sign-on for the dashboard — generic OpenID Connect.

The dashboard has no user database and no passwords: it delegates *who you are*
to an OIDC provider and decides *whether you may in* from an allowlist. Any
compliant provider works (Google, Microsoft Entra ID, Okta, Auth0, Keycloak, …)
because every endpoint is read from the issuer's discovery document — only the
issuer URL and a client id/secret differ between them.

Flow (authorization code):

    /login          → redirect to the provider (state + nonce in the session)
    /auth/callback  → exchange the code for tokens, resolve the identity,
                      check the allowlist, then set a signed session cookie
    /logout         → drop the session

Every other route is blocked until that session exists. Two exceptions, both
deliberate: the login routes themselves, and ``/assets/<file>`` — Instagram
FETCHES media from that URL with no cookies, so guarding it would break
publishing. Asset filenames are unguessable (uuid4), which is what keeps them
private in practice.

**ID token signatures are not verified here**, and that is safe in this flow
specifically: the token is fetched by this server directly from the provider's
token endpoint over TLS (a back-channel request authenticated with the client
secret), never accepted from the browser. OpenID Connect Core §3.1.3.7 allows
skipping signature validation in exactly that case. The issuer, audience,
expiry, and nonce are still checked below. If you ever accept an ID token from
the front channel (implicit/hybrid), this reasoning no longer holds and you must
verify signatures against the provider's JWKS.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from flask import (
    Flask, redirect, render_template, request, session, url_for,
)

from ..config import settings

logger = logging.getLogger("aismm.sso")

# Endpoints reachable without a session. ``asset`` is public on purpose (see the
# module docstring); ``static`` serves the stylesheet used by the login page.
# ``terms``/``privacy`` are the legal pages a platform's app-review crawler must
# be able to fetch with no cookie (TikTok requires both URLs). ``site_verification``
# serves the domain-ownership files (e.g. TikTok's tiktok<code>.txt) the same way.
PUBLIC_ENDPOINTS = {"login", "auth_callback", "logout", "static", "asset", "healthz",
                    "terms", "privacy", "site_verification"}

_SESSION_USER = "sso_user"
_STATE_KEY = "sso_state"
_NONCE_KEY = "sso_nonce"
_NEXT_KEY = "sso_next"
_CLOCK_SKEW = 120  # seconds of leeway on exp

_discovery_cache: dict[str, dict] = {}


# --- provider discovery ------------------------------------------------------ #

def discovery(issuer: str | None = None) -> dict:
    """Fetch (and cache) the issuer's OIDC discovery document."""
    issuer = issuer or settings.auth.issuer
    if issuer not in _discovery_cache:
        url = f"{issuer}/.well-known/openid-configuration"
        with httpx.Client(timeout=15) as client:
            resp = client.get(url)
            resp.raise_for_status()
            _discovery_cache[issuer] = resp.json()
        logger.info("Loaded OIDC discovery from %s", url)
    return _discovery_cache[issuer]


# --- ID token handling ------------------------------------------------------- #

def decode_claims(id_token: str) -> dict:
    """Decode a JWT payload without verifying its signature (see module docstring)."""
    try:
        payload = id_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"Malformed ID token: {exc}") from exc


def validate_claims(claims: dict, *, expected_nonce: str, issuer: str,
                    client_id: str | None = None) -> None:
    """Check issuer, audience, expiry and nonce. Raises ValueError on mismatch."""
    client_id = client_id or settings.auth.client_id
    iss = claims.get("iss", "")
    # Entra's multi-tenant discovery advertises a templated issuer; the token
    # carries the concrete tenant id in `tid`.
    expected_iss = issuer.replace("{tenantid}", claims.get("tid", ""))
    if iss.rstrip("/") != expected_iss.rstrip("/"):
        raise ValueError(f"ID token issuer mismatch: {iss!r} != {expected_iss!r}")

    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if client_id not in audiences:
        raise ValueError("ID token audience does not match this client id")

    exp = claims.get("exp")
    if not exp or float(exp) + _CLOCK_SKEW < time.time():
        raise ValueError("ID token has expired")

    if expected_nonce and claims.get("nonce") != expected_nonce:
        raise ValueError("ID token nonce mismatch — possible replay")


def resolve_email(claims: dict, access_token: str, disco: dict) -> str:
    """Best-effort identity address across providers.

    Google puts it in ``email``; Entra ID often sends ``preferred_username`` (or
    ``upn``) instead and only includes ``email`` when the optional claim is
    configured. Fall back to the userinfo endpoint when the token carries none.
    """
    if claims.get("email_verified") is False:
        raise ValueError("The provider reports this address as unverified")

    for key in ("email", "preferred_username", "upn"):
        value = (claims.get(key) or "").strip()
        if "@" in value:
            return value

    endpoint = disco.get("userinfo_endpoint")
    if endpoint and access_token:
        with httpx.Client(timeout=15) as client:
            resp = client.get(endpoint, headers={"Authorization": f"Bearer {access_token}"})
            resp.raise_for_status()
            info = resp.json()
        for key in ("email", "preferred_username", "upn"):
            value = (info.get(key) or "").strip()
            if "@" in value:
                return value
    raise ValueError("The provider returned no email address for this identity")


# --- session ----------------------------------------------------------------- #

def current_user() -> dict | None:
    return session.get(_SESSION_USER)


def _safe_next(target: str | None) -> str:
    """Where to land after a successful sign-in.

    Only same-app relative redirects (no open redirect), and **prefixed with the
    application root**. ``request.full_path`` is the path Flask sees *after* the
    reverse-proxy prefix has been stripped, so storing it verbatim sent everyone
    behind a prefix to ``/instructions`` instead of ``/aismm/instructions`` after
    logging in. ``url_for`` adds the prefix on its own; a raw stored path does
    not, which is exactly the difference that made the fallback work and the
    remembered destination fail.
    """
    if target and target.startswith("/") and not target.startswith("//"):
        root = (request.script_root or "").rstrip("/")
        if root and not (target == root or target.startswith(f"{root}/")):
            return f"{root}{target}"
        return target
    return url_for("index")


# --- wiring ------------------------------------------------------------------ #

def init_app(app: Flask, cfg=None) -> None:
    """Register the login routes and the guard. A no-op when SSO is disabled.

    ``cfg`` is the ``Settings`` the app was built with — passed in rather than
    read from this module, so there is a single source of truth (and tests that
    patch the dashboard's settings reach the guard too).
    """
    cfg = cfg if cfg is not None else settings
    auth = cfg.auth

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",   # the provider redirects back via GET
        SESSION_COOKIE_SECURE=cfg.dashboard.public_base_url.startswith("https://"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=auth.session_hours),
    )

    @app.context_processor
    def _inject_user():
        return {"sso_enabled": auth.enabled, "sso_user": current_user() if auth.enabled else None,
                "sso_provider": auth.provider_name}

    if not auth.enabled:
        if auth.configured:
            logger.warning("SSO is configured but disabled via AUTH_ENABLED=0 — "
                           "the dashboard is UNAUTHENTICATED.")
        else:
            logger.warning("No SSO configured (AUTH_OIDC_ISSUER/_CLIENT_ID/_CLIENT_SECRET) — "
                           "the dashboard is UNAUTHENTICATED. Do not expose it publicly.")
        return

    if not auth.has_allowlist:
        # Fail closed: without an allowlist, *any* account at the provider (e.g.
        # every Google account on earth) would otherwise be let in.
        logger.error("SSO is enabled but AUTH_ALLOWED_EMAILS/AUTH_ALLOWED_DOMAINS are empty — "
                     "every login will be refused until you set one.")

    @app.before_request
    def _require_login():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if current_user():
            return None
        if request.method == "GET":
            session[_NEXT_KEY] = request.full_path.rstrip("?") or "/"
        return redirect(url_for("login"))

    @app.route("/login")
    def login():
        if current_user():
            # Changing the account at the identity provider alone does not
            # change AISMM's own signed session. Make that transition explicit
            # so a person cannot mistake the first user's workspace for the
            # second user's after switching accounts in the same browser.
            if request.args.get("switch") == "1":
                session.clear()
            else:
                return redirect(url_for("index"))
        if request.args.get("go") != "1":
            # Landing page with the sign-in button, so a bare visit doesn't
            # bounce straight out to the provider.
            return render_template("login.html", provider=auth.provider_name,
                                   error=request.args.get("error", ""))
        try:
            disco = discovery(auth.issuer)
        except Exception as exc:  # noqa: BLE001 - show, don't crash
            logger.exception("OIDC discovery failed")
            return render_template("login.html", provider=auth.provider_name,
                                   error=f"Could not reach the identity provider: {exc}"), 502

        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        session[_STATE_KEY], session[_NONCE_KEY] = state, nonce
        params = {
            "client_id": auth.client_id,
            "response_type": "code",
            "redirect_uri": cfg.auth_redirect_uri,
            "scope": " ".join(auth.scopes),
            "state": state,
            "nonce": nonce,
        }
        return redirect(f"{disco['authorization_endpoint']}?{urlencode(params)}")

    @app.route("/auth/callback")
    def auth_callback():
        if request.args.get("error"):
            return _deny(request.args.get("error_description") or request.args["error"])
        if not request.args.get("state") or request.args["state"] != session.pop(_STATE_KEY, None):
            return _deny("Login state mismatch — please try again.")

        nonce = session.pop(_NONCE_KEY, "")
        try:
            disco = discovery(auth.issuer)
            with httpx.Client(timeout=30) as client:
                resp = client.post(disco["token_endpoint"], data={
                    "grant_type": "authorization_code",
                    "code": request.args.get("code", ""),
                    "redirect_uri": cfg.auth_redirect_uri,
                    "client_id": auth.client_id,
                    "client_secret": auth.client_secret,
                })
                resp.raise_for_status()
                tokens = resp.json()
            claims = decode_claims(tokens.get("id_token", ""))
            validate_claims(claims, expected_nonce=nonce,
                            issuer=disco.get("issuer", auth.issuer), client_id=auth.client_id)
            email = resolve_email(claims, tokens.get("access_token", ""), disco)
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the browser
            logger.warning("SSO login failed: %s", exc)
            return _deny(f"Sign-in failed: {exc}")

        if not auth.allows(email):
            logger.warning("SSO login refused for %s (not on the allowlist)", email)
            return _deny(f"{email} is not authorized to use this dashboard.")

        session[_SESSION_USER] = {"email": email,
                                  "name": claims.get("name") or email,
                                  "at": int(time.time())}
        session.permanent = True
        logger.info("SSO login: %s", email)
        return redirect(_safe_next(session.pop(_NEXT_KEY, None)))

    @app.route("/logout")
    def logout():
        user = current_user()
        session.clear()
        if user:
            logger.info("SSO logout: %s", user.get("email"))
        return render_template("login.html", provider=auth.provider_name,
                               error="", signed_out=True)

    def _deny(message: str):
        session.pop(_SESSION_USER, None)
        return render_template("login.html", provider=auth.provider_name, error=message), 403
