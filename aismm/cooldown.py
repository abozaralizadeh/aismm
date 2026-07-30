"""Per-account publishing cooldowns.

When a platform refuses an action for *volume* reasons — Instagram's code 4
"application request limit reached", or the same thing dressed up as "action is
blocked — we restrict certain activity to protect our community" — trying again
soon makes it worse. Meta extends these blocks when an app keeps knocking.

A schedule of "every 1h" against one Instagram account is 24 posts a day, which
is what triggers it. So after a volume refusal the account is put in a cooldown,
and the orchestrator **skips the run before it starts** rather than browsing,
downloading, converting and generating media only to be refused at the last step.

The deadline lives in the account's ``meta`` — no new table and no new Store
methods, and it survives on both storage backends because ``meta_json`` already
round-trips.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("aismm.cooldown")

META_KEY = "publish_blocked_until"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def remaining_seconds(account) -> int:
    """How long this account is still blocked from publishing. 0 when clear."""
    deadline = _parse((account.meta or {}).get(META_KEY))
    if deadline is None:
        return 0
    return max(int((deadline - _now()).total_seconds()), 0)


def is_active(account) -> bool:
    return remaining_seconds(account) > 0


def start(account, store, seconds: int, *, reason: str = "") -> int:
    """Block publishing for this account for ``seconds``. Returns the seconds set.

    Extends rather than shortens an existing cooldown: two refusals in a row mean
    the platform is less happy, not more.
    """
    current = remaining_seconds(account)
    seconds = max(int(seconds), current)
    meta = dict(account.meta or {})
    meta[META_KEY] = (_now() + timedelta(seconds=seconds)).isoformat()
    account.set_meta(meta)
    store.upsert_account(account)
    logger.warning("Publishing cooldown for %s (%s): %d minutes%s",
                   account.handle or account.external_id, account.platform.value,
                   seconds // 60, f" — {reason}" if reason else "")
    return seconds


def clear(account, store) -> None:
    meta = dict(account.meta or {})
    if meta.pop(META_KEY, None) is not None:
        account.set_meta(meta)
        store.upsert_account(account)
        logger.info("Cleared the publishing cooldown for %s",
                    account.handle or account.external_id)


def describe(account) -> str:
    seconds = remaining_seconds(account)
    if not seconds:
        return ""
    if seconds < 3600:
        return f"{seconds // 60} minute(s)"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
