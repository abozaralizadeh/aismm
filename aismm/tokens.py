"""Keeping a stored OAuth access token usable.

An access token is short-lived on most platforms — X expires one in about two
hours, YouTube in one — which is why an account connected in the afternoon
publishes fine and then answers **401 Unauthorized** the next morning without
anything having changed. Instagram hides the problem for two months at a time
with its long-lived page tokens, so this went unnoticed until X was connected.

The refresh token was captured at connect time and stored from the very first
version; nothing ever spent it. Every call site did::

    access_token, _refresh = store.get_tokens(account.id)

so the discarded half of that tuple was exactly the thing that would have kept
the account alive. :func:`valid_access_token` is now the only supported way to
get a token to call a platform with: it returns the stored one while it is
good, and swaps in a fresh one when it is not.

Refreshing is **best effort**. If it fails — no refresh token, unconfigured app
credentials, a revoked grant — the stored token is returned unchanged so the
platform's own 401 surfaces and tells the operator to reconnect. Failing loudly
here instead would turn "your token expired" into "the run crashed", and a
platform that ignores ``expires_at`` (Instagram sets none) must keep working.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from .models import Account
from .platforms import apps as platform_apps
from .platforms.registry import get_platform
from . import workspaces

logger = logging.getLogger("aismm.tokens")

# Refresh a little before the deadline: a token that expires mid-upload is a
# failed publish, and a chunked video upload is not instant.
REFRESH_SKEW = dt.timedelta(minutes=10)

# Only long enough to cover one token call. Two runs on the same account can be
# in flight at once (the scheduler runs jobs in a thread pool), and X rotates
# its refresh token on use — a concurrent second refresh would spend a token
# that has already been consumed.
_LOCK_TTL = 60
_LOCK_WAIT = 15.0


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def needs_refresh(account: Account, *, now: dt.datetime | None = None,
                  skew: dt.timedelta = REFRESH_SKEW) -> bool:
    """Is this account's access token at or past its useful life?

    No ``expires_at`` means "unknown", which is treated as fine: Instagram
    stores none, and guessing an expiry would refresh tokens that cannot be
    refreshed.
    """
    expires_at = _as_utc(account.expires_at)
    if expires_at is None:
        return False
    return (now or dt.datetime.now(dt.timezone.utc)) >= expires_at - skew


def describe_expiry(account: Account) -> str:
    """Human phrasing for the dashboard: "in 43 minutes", "expired 2 hours ago"."""
    expires_at = _as_utc(account.expires_at)
    if expires_at is None:
        return "no expiry recorded"
    delta = expires_at - dt.datetime.now(dt.timezone.utc)
    seconds = abs(delta.total_seconds())
    # Rounded, not floored: an hour-long token read a moment after it was minted
    # is 3599.9 seconds, and "in 59 minutes" reads like it is already decaying.
    if seconds < 90:
        amount = f"{round(seconds)} seconds"
    elif seconds < 5400:
        amount = f"{round(seconds / 60)} minutes"
    elif seconds < 172800:
        amount = f"{round(seconds / 3600)} hours"
    else:
        amount = f"{round(seconds / 86400)} days"
    return f"in {amount}" if delta.total_seconds() > 0 else f"expired {amount} ago"


def _store_bundle(account: Account, store, bundle) -> str:
    """Persist a refreshed bundle and return its access token.

    A platform that does not return a new refresh token keeps the old one — X
    rotates and sends a replacement, Google usually does not send one at all,
    and blanking it would make the account unrefreshable from then on.
    """
    expires_at = None
    if bundle.expires_in:
        expires_at = (dt.datetime.now(dt.timezone.utc)
                      + dt.timedelta(seconds=int(bundle.expires_in)))
    account.expires_at = expires_at
    _old_access, old_refresh = store.get_tokens(account.id)
    store.upsert_account(account, access_token=bundle.access_token,
                         refresh_token=bundle.refresh_token or old_refresh)
    return bundle.access_token


async def _refresh(account: Account, store, refresh_token: str) -> str | None:
    # OAuth apps are workspace-private. Accounts created before workspaces have
    # no concrete id, but belong to the legacy/administrator workspace.
    workspace = (store.get_workspace(account.workspace_id) if account.workspace_id
                 else workspaces.legacy_workspace(store))
    scope = workspaces.scope_for(workspace)
    creds = platform_apps.resolve_creds(account.platform, store,
                                        (account.meta or {}).get("app_id", ""), scope,
                                        allow_env=workspaces.can_use_deployment_config(
                                            store, workspace))
    if not creds.configured:
        logger.warning("Cannot refresh %s (%s): no app credentials are configured for it",
                       account.handle or account.external_id, account.platform.value)
        return None
    platform = get_platform(account.platform, creds)
    bundle = await platform.refresh(refresh_token)
    if not bundle.access_token:
        logger.warning("Refresh for %s returned no access token",
                       account.handle or account.external_id)
        return None
    token = _store_bundle(account, store, bundle)
    logger.info("Refreshed the %s token for %s (now expires %s)", account.platform.value,
                account.handle or account.external_id, describe_expiry(account))
    return token


async def valid_access_token(account: Account, store) -> str:
    """The access token to call ``account``'s platform with, refreshed if stale.

    Always prefer this over ``store.get_tokens(...)[0]`` at a call site that is
    about to hit a platform API. It mutates ``account`` in place when it
    refreshes, so the caller's copy carries the new ``expires_at``.
    """
    access_token, refresh_token = store.get_tokens(account.id)
    if not needs_refresh(account):
        return access_token
    if not refresh_token:
        logger.warning("The %s token for %s has expired and there is no refresh token — "
                       "reconnect the account in the dashboard.", account.platform.value,
                       account.handle or account.external_id)
        return access_token

    # Serialize refreshes per account: a rotating refresh token spent twice
    # invalidates the grant, which is worse than the expiry we are fixing.
    lock_key = f"token:{account.id}"
    if not store.acquire_lock(lock_key, ttl_seconds=_LOCK_TTL):
        return await _await_other_refresh(account, store, access_token)
    try:
        # Re-check under the lock. Waiting for the lock is not the only way to
        # arrive late: the account object in hand was read before the wait, so
        # a refresh that finished in between leaves it claiming an expiry that
        # is no longer true. Refreshing on that stale copy spends the *new*
        # rotated refresh token — the exact grant-killing double spend the lock
        # is here to prevent.
        latest = store.get_account(account.id) or account
        if not needs_refresh(latest):
            account.expires_at = latest.expires_at
            current, _ = store.get_tokens(account.id)
            return current or access_token
        _stale, refresh_token = store.get_tokens(account.id)
        fresh = await _refresh(account, store, refresh_token)
        return fresh or access_token
    except Exception as exc:  # noqa: BLE001 — the platform's own 401 is clearer
        logger.warning("Could not refresh the %s token for %s: %s — the stored token will be "
                       "used and may be rejected.", account.platform.value,
                       account.handle or account.external_id, exc)
        return access_token
    finally:
        store.release_lock(lock_key)


async def _await_other_refresh(account: Account, store, current: str) -> str:
    """Another run is refreshing this account; use what it stores."""
    deadline = asyncio.get_event_loop().time() + _LOCK_WAIT
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
        latest = store.get_account(account.id)
        if latest is None:
            break
        token, _refresh_token = store.get_tokens(account.id)
        if token and token != current:
            account.expires_at = latest.expires_at
            return token
        if not needs_refresh(latest):
            return token or current
    logger.warning("Timed out waiting for another run to refresh %s",
                   account.handle or account.external_id)
    return current


def valid_access_token_sync(account: Account, store) -> str:
    """Blocking wrapper, for the dashboard and CLI paths that are not async."""
    return asyncio.run(valid_access_token(account, store))
