"""Shared fixtures — and a deterministic environment for the whole suite.

``aismm.config`` reads env (and ``.env``) **at import time**, so a developer's
real ``.env`` would otherwise leak into the tests: a public
``DASHBOARD_BASE_URL`` breaks the Instagram guard test, a live
``REVERSE_PROXY_PREFIX`` or ``AUTH_OIDC_*`` changes how the dashboard is built,
and a non-Fernet ``AISMM_TOKEN_KEY`` fails the store outright.

Setting these here works because pytest imports conftest **before** the test
modules that import ``aismm``, and ``python-dotenv`` does not override variables
that already exist in the environment.
"""
import os
import tempfile

from cryptography.fernet import Fernet

_TEST_ENV = {
    # Valid Fernet key so token encryption works regardless of the local .env.
    "AISMM_TOKEN_KEY": Fernet.generate_key().decode(),
    # Keep generated URLs local: the Instagram integration must still refuse
    # localhost media URLs (that is what test_platforms asserts).
    "DASHBOARD_BASE_URL": "http://127.0.0.1:8787",
    "REVERSE_PROXY_PREFIX": "",
    # Dashboard tests build their own SSO settings; the guard is off by default.
    "AUTH_ENABLED": "0",
    "AUTH_OIDC_ISSUER": "",
    "AUTH_OIDC_CLIENT_ID": "",
    "AUTH_OIDC_CLIENT_SECRET": "",
    "AUTH_ALLOWED_EMAILS": "",
    "AUTH_ALLOWED_DOMAINS": "",
    "AUTH_OWNER_EMAILS": "",
    # Never write into the developer's real data dir.
    "AISMM_DATA_DIR": tempfile.mkdtemp(prefix="aismm-tests-"),
    # Sora failover attempts must not depend on a real pool being configured.
    "SORA_MAX_ATTEMPTS": "0",
    # Metrics-refresh window is read at import; pin it so a developer's .env
    # cannot change what the feedback-loop tests see.
    "METRICS_REFRESH_DAYS": "30",
    # Never touch a real storage account — a SandBox-style .env shares one under
    # the lowercase `connection_string` name, which the store would pick up.
    "AZURE_STORAGE_CONNECTION_STRING": "",
    "connection_string": "",
    "STORE_BACKEND": "local",
    # Platform app credentials: a developer's real .env would otherwise decide
    # whether ".env is configured", which changes credential resolution.
    "INSTAGRAM_APP_ID": "", "INSTAGRAM_APP_SECRET": "",
    "TWITTER_CLIENT_ID": "", "TWITTER_CLIENT_SECRET": "",
    "TWITTER_API_KEY": "", "TWITTER_API_SECRET": "",
    "GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": "",
    "TIKTOK_CLIENT_KEY": "", "TIKTOK_CLIENT_SECRET": "",
}
os.environ.update(_TEST_ENV)

import pytest  # noqa: E402

from aismm.store.local_store import LocalStore  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    """A LocalStore backed by a throwaway SQLite file."""
    return LocalStore(db_url=f"sqlite:///{tmp_path/'test.sqlite'}")
