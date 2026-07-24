"""Generic OAuth 2.0 authorization-code (+ PKCE) helpers.

Platforms compose these with their own endpoints/scopes. The dashboard drives the
browser redirect flow: it builds an authorize URL, the user approves on the
platform, the platform redirects back to ``/oauth/<platform>/callback`` with a
``code``, and we exchange it for tokens.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str = ""
    expires_in: int | None = None
    scope: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: dict) -> "TokenBundle":
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_in=data.get("expires_in"),
            scope=data.get("scope", ""),
            raw=data,
        )


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def random_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(
    auth_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    code_challenge: str | None = None,
    scope_sep: str = " ",
    extra_params: dict | None = None,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope_sep.join(scopes),
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if extra_params:
        params.update(extra_params)
    return f"{auth_endpoint}?{urlencode(params)}"


async def exchange_code(
    token_endpoint: str,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str | None = None,
    auth_style: str = "body",  # "body" | "basic"
    extra: dict | None = None,
) -> TokenBundle:
    """Exchange an authorization ``code`` for tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    if extra:
        data.update(extra)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    auth = None
    if auth_style == "basic":
        auth = (client_id, client_secret)
    else:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(token_endpoint, data=data, headers=headers, auth=auth)
        resp.raise_for_status()
        return TokenBundle.from_response(resp.json())


async def refresh_token(
    token_endpoint: str,
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    auth_style: str = "body",
    extra: dict | None = None,
) -> TokenBundle:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if extra:
        data.update(extra)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    auth = None
    if auth_style == "basic":
        auth = (client_id, client_secret)
    else:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(token_endpoint, data=data, headers=headers, auth=auth)
        resp.raise_for_status()
        return TokenBundle.from_response(resp.json())
