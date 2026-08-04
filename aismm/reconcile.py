"""Repair runs that published but were recorded as failures.

Instagram's ``media_publish`` answered ``code=4 · error_subcode=2207051`` and
posted the content anyway. Every one of those runs is stored as ``failed`` with a
live post on the account, and the [publish ledger](publish_ledger.py) — which
only starts recording from the moment it was introduced — has no entry for them.
Two consequences worth fixing rather than living with:

* the dashboard reports failures for posts that are on the account;
* the duplicate guard cannot block a re-post of something it never saw published,
  so an instruction whose memory still lists the item as outstanding would post
  it a third time.

This reads what is ACTUALLY on the account and reconciles both. A run is matched
to a live post by its stored caption — the caption is what the run recorded and
what Graph gives back, and it is unique enough in practice. Read-only against the
platform: it publishes nothing and deletes nothing.
"""
from __future__ import annotations

import asyncio
import logging

from . import publish_ledger, tokens
from .assets import exists as asset_exists
from .models import PlatformName, RunStatus
from .platforms.instagram import _caption_key
from .platforms.registry import get_platform

logger = logging.getLogger("aismm.reconcile")

# How many recent posts to read per account, and how far back through the run
# table to look for mismatches.
POST_LIMIT = 50
RUN_LIMIT = 200


def _published_posts(account, store) -> list[dict]:
    """Recent posts actually on the account. Instagram only, for now."""
    if account.platform is not PlatformName.instagram:
        return []
    access_token = tokens.valid_access_token_sync(account, store)
    if not access_token:
        logger.warning("No token for %s — reconnect it to reconcile this account",
                       account.handle or account.external_id)
        return []
    platform = get_platform(account.platform)
    return asyncio.run(platform.list_media(
        access_token, account, limit=POST_LIMIT,
        fields="id,caption,permalink,timestamp"))


def reconcile_account(account, store, *, apply: bool = False) -> dict:
    """Match this account's failed runs against posts that are really live.

    With ``apply`` false this only reports — the default, so an operator sees what
    would change before it changes.
    """
    posts = _published_posts(account, store)
    by_caption = {}
    for post in posts:
        key = _caption_key(post.get("caption"))
        if key:
            by_caption.setdefault(key, post)

    runs = store.list_runs(limit=RUN_LIMIT, account_id=account.id,
                           status=RunStatus.failed)
    repaired, seeded = [], 0
    for run in runs:
        key = _caption_key(run.caption)
        post = by_caption.get(key) if key else None
        if not post:
            continue

        repaired.append((run, post))
        if not apply:
            continue

        run.status = RunStatus.published
        run.external_url = post.get("permalink", "")
        run.error = ""
        run.log = (run.log + f"\nRECONCILED: this run's post IS live at "
                             f"{post.get('permalink', post.get('id', '?'))} — Instagram "
                             f"reported a rate-limit error after publishing it.").strip()
        store.update_run(run)

        # Seed the ledger so the duplicate guard knows about this post. Needs the
        # media the run published; a wiped asset simply cannot be fingerprinted.
        paths = [p for p in [run.asset_path] if p and asset_exists(p)]
        if paths:
            digest = publish_ledger.fingerprint(paths)
            if digest and not publish_ledger.find(account, digest):
                publish_ledger.record(account, store, digest,
                                      url=post.get("permalink", ""),
                                      external_id=post.get("id", ""),
                                      instruction_id=run.instruction_id)
                seeded += 1

    return {"account": account.handle or account.external_id,
            "live_posts": len(posts), "failed_runs": len(runs),
            "repaired": [(r.id, p.get("permalink", "")) for r, p in repaired],
            "ledger_seeded": seeded, "applied": apply}


def reconcile_all(store, *, apply: bool = False) -> list[dict]:
    reports = []
    for account in store.list_accounts():
        if account.platform is not PlatformName.instagram:
            continue
        try:
            reports.append(reconcile_account(account, store, apply=apply))
        except Exception as exc:  # noqa: BLE001 - one bad account must not stop the rest
            logger.error("Could not reconcile %s: %s",
                         account.handle or account.external_id, exc)
            reports.append({"account": account.handle or account.external_id,
                            "error": str(exc)})
    return reports
