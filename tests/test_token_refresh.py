"""Access tokens expire; the stored refresh token must actually be spent.

The reported failure: an X account connected one afternoon published fine, and
the next morning every call — reads and the publish alike — answered 401
Unauthorized. Nothing had changed. X access tokens last about two hours.

The refresh token had been captured at connect time and stored since the first
version, and *nothing ever used it*: every call site read
``access_token, _refresh = store.get_tokens(...)`` and threw the second half
away. Instagram's 60-day page tokens hid this until X was connected.
"""
import asyncio
import datetime as dt

import pytest

from aismm import tokens
from aismm.auth.oauth import TokenBundle
from aismm.models import Account, PlatformName
from aismm import workspaces

UTC = dt.timezone.utc


def _account(store, *, expires_in_seconds=None, refresh="r0", platform=PlatformName.twitter):
    expires_at = None
    if expires_in_seconds is not None:
        expires_at = dt.datetime.now(UTC) + dt.timedelta(seconds=expires_in_seconds)
    return store.upsert_account(
        Account(platform=platform, handle="abo0zar", external_id="9", expires_at=expires_at),
        access_token="old-access", refresh_token=refresh)


@pytest.fixture()
def refreshed(monkeypatch):
    """Stub the platform refresh; record what it was called with."""
    calls = []

    class _Creds:
        configured = True
        client_id = "cid"
        client_secret = "secret"

    class _Platform:
        async def refresh(self, refresh_token):
            calls.append(refresh_token)
            return TokenBundle(access_token="new-access", refresh_token="r1", expires_in=7200)

    monkeypatch.setattr(tokens.platform_apps, "resolve_creds",
                        lambda *a, **kw: _Creds())
    monkeypatch.setattr(tokens, "get_platform", lambda *a, **kw: _Platform())
    return calls


# --- when to refresh ----------------------------------------------------------------- #

def test_a_fresh_token_is_used_as_is(store, refreshed):
    account = _account(store, expires_in_seconds=7200)
    assert asyncio.run(tokens.valid_access_token(account, store)) == "old-access"
    assert refreshed == []


def test_an_expired_token_is_refreshed(store, refreshed):
    """The overnight case: connected yesterday, called this morning."""
    account = _account(store, expires_in_seconds=-3600)
    assert asyncio.run(tokens.valid_access_token(account, store)) == "new-access"
    assert refreshed == ["r0"]


def test_refresh_uses_the_account_workspace_app_credentials(store, monkeypatch):
    workspace = workspaces.create(store, "Mine", "me@example.com")
    account = _account(store, expires_in_seconds=-60)
    account.workspace_id = workspace.id
    store.upsert_account(account)
    seen = {}

    class _Creds:
        configured = False

    def resolve(*args, **kwargs):
        seen["scope"] = args[3]
        seen["allow_env"] = kwargs["allow_env"]
        return _Creds()

    monkeypatch.setattr(tokens.platform_apps, "resolve_creds", resolve)
    asyncio.run(tokens.valid_access_token(account, store))
    assert seen == {"scope": workspace.id, "allow_env": True}


def test_a_token_about_to_expire_is_refreshed_early(store, refreshed):
    """A token that dies mid-upload is a failed publish; a chunked video is not instant."""
    account = _account(store, expires_in_seconds=60)
    assert asyncio.run(tokens.valid_access_token(account, store)) == "new-access"


def test_an_account_with_no_recorded_expiry_is_left_alone(store, refreshed):
    """Instagram stores none — guessing an expiry would refresh what cannot be."""
    account = _account(store, expires_in_seconds=None, platform=PlatformName.instagram)
    assert asyncio.run(tokens.valid_access_token(account, store)) == "old-access"
    assert refreshed == []


def test_a_naive_expiry_is_read_as_utc(store, refreshed):
    """SQLite hands back naive datetimes; comparing them to an aware now() raises."""
    account = _account(store, expires_in_seconds=-3600)
    account.expires_at = account.expires_at.replace(tzinfo=None)
    assert asyncio.run(tokens.valid_access_token(account, store)) == "new-access"


# --- what gets persisted -------------------------------------------------------------- #

def test_the_new_tokens_are_stored(store, refreshed):
    account = _account(store, expires_in_seconds=-60)
    asyncio.run(tokens.valid_access_token(account, store))
    access, refresh = store.get_tokens(account.id)
    assert (access, refresh) == ("new-access", "r1")


def test_the_new_expiry_is_recorded(store, refreshed):
    account = _account(store, expires_in_seconds=-60)
    asyncio.run(tokens.valid_access_token(account, store))
    stored = store.get_account(account.id)
    assert stored.expires_at > dt.datetime.now(UTC).replace(tzinfo=None) + dt.timedelta(hours=1)
    # The caller's copy must carry it too, or it refreshes again on the next call.
    assert not tokens.needs_refresh(account)


def test_a_platform_that_returns_no_new_refresh_token_keeps_the_old_one(store, monkeypatch):
    """Google usually omits it; blanking it makes the account unrefreshable."""
    class _Creds:
        configured = True

    class _Platform:
        async def refresh(self, refresh_token):
            return TokenBundle(access_token="new-access", refresh_token="", expires_in=3600)

    monkeypatch.setattr(tokens.platform_apps, "resolve_creds", lambda *a, **kw: _Creds())
    monkeypatch.setattr(tokens, "get_platform", lambda *a, **kw: _Platform())
    account = _account(store, expires_in_seconds=-60, refresh="keep-me")
    asyncio.run(tokens.valid_access_token(account, store))
    assert store.get_tokens(account.id) == ("new-access", "keep-me")


def test_the_app_that_minted_the_token_is_the_one_asked_to_refresh(store, monkeypatch):
    """A dashboard-managed app's credentials, not whatever .env holds."""
    seen = {}

    class _Creds:
        configured = True

    def _resolve(platform, _store, app_id="", *args, **kwargs):
        seen["app_id"] = app_id
        return _Creds()

    class _Platform:
        async def refresh(self, refresh_token):
            return TokenBundle(access_token="new-access", expires_in=3600)

    monkeypatch.setattr(tokens.platform_apps, "resolve_creds", _resolve)
    monkeypatch.setattr(tokens, "get_platform", lambda *a, **kw: _Platform())
    account = _account(store, expires_in_seconds=-60)
    account.set_meta({"app_id": "app-7"})
    store.upsert_account(account)
    asyncio.run(tokens.valid_access_token(account, store))
    assert seen["app_id"] == "app-7"


# --- failure is best effort ------------------------------------------------------------ #
# The platform's own 401 names the problem better than an exception from here,
# and a run must not crash because a refresh was refused.

def test_a_refusal_falls_back_to_the_stored_token(store, monkeypatch):
    class _Creds:
        configured = True

    class _Platform:
        async def refresh(self, refresh_token):
            raise RuntimeError("invalid_grant")

    monkeypatch.setattr(tokens.platform_apps, "resolve_creds", lambda *a, **kw: _Creds())
    monkeypatch.setattr(tokens, "get_platform", lambda *a, **kw: _Platform())
    account = _account(store, expires_in_seconds=-60)
    assert asyncio.run(tokens.valid_access_token(account, store)) == "old-access"


def test_an_account_with_no_refresh_token_is_not_an_error(store, refreshed):
    account = _account(store, expires_in_seconds=-60, refresh="")
    assert asyncio.run(tokens.valid_access_token(account, store)) == "old-access"
    assert refreshed == []


def test_unconfigured_app_credentials_do_not_raise(store, monkeypatch):
    class _Creds:
        configured = False

    monkeypatch.setattr(tokens.platform_apps, "resolve_creds", lambda *a, **kw: _Creds())
    account = _account(store, expires_in_seconds=-60)
    assert asyncio.run(tokens.valid_access_token(account, store)) == "old-access"


def test_the_lock_is_released_after_a_failed_refresh(store, monkeypatch):
    """Otherwise the next run waits out the TTL for a refresh that already ended."""
    class _Creds:
        configured = True

    class _Platform:
        async def refresh(self, refresh_token):
            raise RuntimeError("nope")

    monkeypatch.setattr(tokens.platform_apps, "resolve_creds", lambda *a, **kw: _Creds())
    monkeypatch.setattr(tokens, "get_platform", lambda *a, **kw: _Platform())
    account = _account(store, expires_in_seconds=-60)
    asyncio.run(tokens.valid_access_token(account, store))
    assert store.acquire_lock(f"token:{account.id}", ttl_seconds=5)


# --- concurrency ----------------------------------------------------------------------- #

def test_two_runs_do_not_both_spend_a_rotating_refresh_token(store, refreshed):
    """X rotates the refresh token on use; spending it twice kills the grant."""
    account = _account(store, expires_in_seconds=-60)
    other = store.get_account(account.id)

    async def both():
        return await asyncio.gather(tokens.valid_access_token(account, store),
                                    tokens.valid_access_token(other, store))

    results = asyncio.run(both())
    assert refreshed == ["r0"]                    # exactly one refresh
    assert results == ["new-access", "new-access"]  # both callers get the fresh token


# --- the readable expiry for the dashboard --------------------------------------------- #

def test_expiry_is_described_for_a_human(store):
    account = _account(store, expires_in_seconds=3600)
    assert tokens.describe_expiry(account) == "in 60 minutes"
    account.expires_at = dt.datetime.now(UTC) - dt.timedelta(hours=3)
    assert tokens.describe_expiry(account) == "expired 3 hours ago"
    account.expires_at = None
    assert tokens.describe_expiry(account) == "no expiry recorded"
