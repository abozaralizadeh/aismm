"""User data deletion: the platforms' deletion callbacks, and the purge behind them.

Meta requires every app that accesses user data to offer "a way for users to request
that their data be deleted": either a Data Deletion Request Callback URL or a page of
instructions. This module serves both routes, and the same purge sits behind each:

* **Meta (Instagram + Facebook)** — ``POST /data-deletion/meta``. When someone removes
  the app from Facebook and asks for their data to be deleted, Meta POSTs a
  ``signed_request`` (``<sig>.<payload>``, both base64url; the signature is
  HMAC-SHA256 of the payload segment keyed with the APP SECRET). We must answer with
  JSON ``{"url": <status page>, "confirmation_code": <code>}``. The payload names the
  person by their APP-SCOPED Facebook user id, which an account row never carried (it
  holds the Instagram account id and the Page id), so it is now recorded at connect
  (``account.meta["provider_user_id"]``). Accounts connected before that are matched
  through ``/debug_token``, whose ``user_id`` is the person who granted the stored Page
  token.
* **TikTok** — ``POST /data-deletion/tiktok``, a webhook. TikTok sends
  ``authorization.removed`` when someone disconnects the app, with ``user_openid`` (an
  account's ``external_id``) and a ``TikTok-Signature: t=<unix>,s=<hex>`` header, an
  HMAC-SHA256 of ``"{t}.{raw body}"`` keyed with the client secret.
* **X, YouTube, LinkedIn, Reddit** offer no deletion callback at all, so they are
  covered the other way the requirement allows: the public ``/data-deletion`` page says
  how to ask, and the operator deletes the data from the Accounts page ("Delete all
  data"), which runs the same purge and gives the same confirmation code.

**What is deleted** for each matched account: the account row (its encrypted tokens and
everything in ``meta`` — handles, ledgers of what was published and answered, cooldowns,
scopes), every run of it (captions, permalinks, platform metrics, logs), every staged
post or reply (which can quote other people's comments), and its place in every
instruction. Generated media is the operator's own work and stays; posts already
published live on the platform, where their owner deletes them.

**The user id itself is never stored.** ``DeletionRequest.user_ref`` is a sha256 of it,
and accounts are matched by hashing their ids the same way, so a request can be traced
without keeping the identifier the person asked us to forget. The confirmation code is
random and unguessable because the status page is public.

The purge runs in a background thread so Meta gets its answer immediately; a restart
mid-way leaves the request ``received``, and ``process_pending`` (run at scheduler
start) finishes it. Every step is idempotent, so running one twice is harmless.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import datetime, timezone

from .config import settings
from .models import Account, DeletionRequest, PlatformName
from .platforms import apps as platform_apps
from .platforms.instagram import PROVIDER_USER_KEY

logger = logging.getLogger("aismm.data_deletion")

META_PLATFORMS = (PlatformName.instagram, PlatformName.facebook)
TIKTOK_EVENT = "authorization.removed"
TIKTOK_TOLERANCE_SECONDS = 300          # replay window for the signed timestamp

RECEIVED, COMPLETED, NOTHING_FOUND, FAILED = "received", "completed", "nothing_found", "failed"


class InvalidSignature(Exception):
    """The request was not signed by any app this deployment knows."""


def user_ref(source: str, user_id: str) -> str:
    return hashlib.sha256(f"{source}:{user_id}".encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- who is allowed to call ---------------------------------------------------------- #

def app_secrets(store, platforms) -> list[tuple[str, str]]:
    """``(client_id, client_secret)`` of every configured app for these platforms.

    The callback carries no workspace and no app id we could trust before checking the
    signature, so every app is tried: ``.env`` and every dashboard-managed app.
    """
    found: dict[str, str] = {}
    for platform in platforms:
        creds = [platform_apps.env_creds(platform)]
        try:
            creds += [platform_apps.app_creds(a, store) for a in store.list_platform_apps(platform)]
        except Exception as exc:  # noqa: BLE001 - .env alone still verifies
            logger.warning("Could not list %s apps for signature checks: %s", platform.value, exc)
        for c in creds:
            if c.configured:
                found.setdefault(c.client_id, c.client_secret)
    return list(found.items())


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def parse_meta_signed_request(signed_request: str,
                              secrets: list[tuple[str, str]]) -> tuple[dict, str]:
    """``(payload, app_id)`` of a valid Meta ``signed_request``, else InvalidSignature."""
    try:
        sig_part, payload_part = (signed_request or "").split(".", 1)
        signature = _b64url(sig_part)
        payload = json.loads(_b64url(payload_part))
    except (ValueError, TypeError) as exc:
        raise InvalidSignature(f"malformed signed_request: {exc}") from None
    if str(payload.get("algorithm", "")).upper() != "HMAC-SHA256":
        raise InvalidSignature("unexpected signing algorithm")
    for app_id, secret in secrets:
        expected = hmac.new(secret.encode(), payload_part.encode(), hashlib.sha256).digest()
        if hmac.compare_digest(expected, signature):
            return payload, app_id
    raise InvalidSignature("signature matches no configured Meta app")


def verify_tiktok_signature(header: str, body: bytes, secrets: list[tuple[str, str]], *,
                            now: float | None = None) -> str:
    """The client key whose secret signed this webhook, else InvalidSignature."""
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    stamp, signature = parts.get("t", ""), parts.get("s", "")
    if not stamp.isdigit() or not signature:
        raise InvalidSignature("missing TikTok-Signature")
    if abs((now if now is not None else time.time()) - int(stamp)) > TIKTOK_TOLERANCE_SECONDS:
        raise InvalidSignature("stale TikTok-Signature timestamp")
    signed = stamp.encode() + b"." + (body or b"")
    for client_key, secret in secrets:
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature):
            return client_key
    raise InvalidSignature("signature matches no configured TikTok app")


# --- the request ---------------------------------------------------------------------- #

def status_url(code: str) -> str:
    return settings.dashboard.external_url(f"data-deletion/status/{code}")


def submit(store, *, source: str, user_id: str = "", account_id: str = "", app_id: str = "",
           background: bool = True) -> DeletionRequest:
    """Record a deletion request and start it. Returns it at once (its id is the code).

    A platform may retry the same callback; an identical request still being worked on
    is returned rather than duplicated, so the person gets one code.
    """
    ref = user_ref("account", account_id) if source == "operator" else user_ref(source, user_id)
    for open_req in store.list_deletion_requests(status=RECEIVED):
        if open_req.source == source and open_req.user_ref == ref:
            return open_req
    request = store.upsert_deletion_request(
        DeletionRequest(source=source, user_ref=ref, app_id=app_id))
    logger.info("Data deletion request %s received (source=%s)", request.id, source)
    if not background:
        return process(store, request.id) or request
    threading.Thread(target=_process_safely, args=(store, request.id),
                     name=f"data-deletion-{request.id}", daemon=True).start()
    return request


def _process_safely(store, code: str) -> None:
    try:
        process(store, code)
    except Exception:  # noqa: BLE001 - a daemon thread must log, never die silently
        logger.exception("Data deletion %s crashed; it will be retried at the next restart",
                         code)


def process(store, code: str, *, resolve_meta_user=None) -> DeletionRequest | None:
    """Find the request's accounts and purge them. Safe to call again on the same code."""
    request = store.get_deletion_request(code)
    if request is None or request.status not in (RECEIVED, FAILED):
        return request
    try:
        accounts = matching_accounts(store, request, resolve_meta_user=resolve_meta_user)
        accounts_n = runs_n = staged_n = 0
        for account in accounts:
            counts = purge_account(store, account)
            accounts_n += 1
            runs_n += counts["runs"]
            staged_n += counts["staged"]
        request.accounts_deleted += accounts_n
        request.runs_deleted += runs_n
        request.staged_deleted += staged_n
        request.status = COMPLETED if accounts_n else NOTHING_FOUND
        request.detail = "" if accounts_n else (
            "No connected account matches this request, so nothing was stored about it.")
    except Exception as exc:  # noqa: BLE001 - record it; the boot sweep retries
        logger.exception("Data deletion %s failed", code)
        request.status = FAILED
        request.detail = f"Deletion did not finish ({type(exc).__name__}); it will be retried."
    request.completed_at = _now()
    store.upsert_deletion_request(request)
    logger.info("Data deletion %s %s: %d account(s), %d run(s), %d staged item(s)",
                code, request.status, request.accounts_deleted, request.runs_deleted,
                request.staged_deleted)
    return request


def process_pending(store) -> int:
    """Finish requests a restart interrupted (and retry failed ones). Never raises."""
    done = 0
    try:
        pending = (store.list_deletion_requests(status=RECEIVED)
                   + store.list_deletion_requests(status=FAILED))
        for request in pending:
            process(store, request.id)
            done += 1
    except Exception as exc:  # noqa: BLE001 - must never block startup
        logger.warning("Could not finish pending data deletions: %s", exc)
    return done


# --- matching -------------------------------------------------------------------------- #

def matching_accounts(store, request: DeletionRequest, *,
                      resolve_meta_user=None) -> list[Account]:
    accounts = store.list_accounts()
    if request.source == "operator":
        return [a for a in accounts if user_ref("account", a.id) == request.user_ref]
    if request.source == "tiktok":
        return [a for a in accounts if a.platform == PlatformName.tiktok
                and user_ref("tiktok", a.external_id or "") == request.user_ref]
    if request.source != "meta":
        return []
    resolve = resolve_meta_user or (lambda acct: _meta_user_from_token(store, acct))
    matched = []
    for account in accounts:
        if account.platform not in META_PLATFORMS:
            continue
        meta = account.meta or {}
        known = str(meta.get(PROVIDER_USER_KEY, "") or "")
        if not known:
            # Connected before the id was recorded: ask Graph who granted the token,
            # and keep the answer so the next request does not have to.
            known = resolve(account) or ""
            if known:
                account.set_meta({**meta, PROVIDER_USER_KEY: known})
                store.upsert_account(account)
        if known and user_ref("meta", known) == request.user_ref:
            matched.append(account)
    return matched


def _meta_user_from_token(store, account: Account) -> str:
    """Who granted this account's stored Page token, per Graph's ``/debug_token``."""
    from .platforms.registry import get_platform

    try:
        access, _refresh = store.get_tokens(account.id)
        if not access:
            return ""
        app_id = (account.meta or {}).get("app_id", "")
        creds = platform_apps.resolve_creds(account.platform, store, app_id or None)
        if not creds.configured:
            return ""
        info = asyncio.run(get_platform(PlatformName.instagram, creds).inspect_token(access))
        return str(info.get("user_id", "") or "")
    except Exception as exc:  # noqa: BLE001 - unmatched is reported, never fatal
        logger.warning("Could not resolve the Facebook user of %s: %s", account.id, exc)
        return ""


# --- the purge ------------------------------------------------------------------------- #

def purge_account(store, account: Account) -> dict:
    """Delete everything stored about one connected account. Idempotent."""
    staged = store.delete_staged_for_account(account.id)
    runs = store.delete_runs_for_account(account.id)
    unlinked = 0
    for instruction in store.list_instructions():
        ids = instruction.account_ids
        if account.id in ids:
            instruction.set_account_ids([i for i in ids if i != account.id])
            store.upsert_instruction(instruction)
            unlinked += 1
    store.delete_account(account.id)
    logger.info("Deleted the data of %s account %s: %d run(s), %d staged item(s), "
                "unlinked from %d instruction(s)", account.platform.value, account.id,
                runs, staged, unlinked)
    return {"runs": runs, "staged": staged, "instructions": unlinked}
