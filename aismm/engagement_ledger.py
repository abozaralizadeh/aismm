"""What this account has already REPLIED to — the engagement duplicate guard.

An engagement run reads the same comment thread every time it fires. A cron
"respond to comments" instruction firing hourly would re-read yesterday's
comments and reply to each of them again, every hour, forever — the exact same
failure mode the [publish ledger](publish_ledger.py) was written for, one level
over. So the fact that must never be lost here is "we already answered this
comment", and — same lesson as the publish ledger and the publish-mode gate — a
guarantee that must hold on every path lives in code, not in model-written prose.

The key is the TARGET id (the comment / mention / tweet being answered), not the
reply text: the agent rewrites its wording each run, but the thing it must not
answer twice is the same upstream item. ``target_type`` is part of the key so a
future DM to the same numeric id as a comment can't collide.

Stored in the account's ``meta`` like the [cooldown](cooldown.py) deadline and
the publish ledger — no new table, no new Store methods, round-trips on both
backends. Bounded to ``MAX_ENTRIES`` so ``meta_json`` stays inside Azure Table's
64KB property cap.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("aismm.engagement_ledger")

META_KEY = "answered_targets"

# A comment thread a cron bot watches turns over faster than a post history, but
# a target answered long ago will not resurface as "new", so a moderate window
# is plenty. Kept well inside the meta size budget.
MAX_ENTRIES = 300
# How long an answered-target fingerprint blocks a re-reply. Long enough to
# outlast any realistic re-read cadence.
DEFAULT_TTL_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def key(target_type: str, target_id: str) -> str:
    """Identity of a thing answered: its kind + id. Empty id → empty key."""
    tid = str(target_id or "").strip()
    if not tid:
        return ""
    return f"{(target_type or 'comment').strip().lower()}:{tid}"


def _entries(account) -> list[dict]:
    raw = (account.meta or {}).get(META_KEY)
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def find(account, k: str, *, ttl_days: int = DEFAULT_TTL_DAYS) -> dict | None:
    """The ledger entry for this target key, if answered recently."""
    if not k:
        return None
    cutoff = _now() - timedelta(days=ttl_days)
    for entry in _entries(account):
        if entry.get("k") != k:
            continue
        stamp = _parse(entry.get("at"))
        if stamp is None or stamp >= cutoff:
            return entry
    return None


def answered(account, target_type: str, target_id: str, *,
             ttl_days: int = DEFAULT_TTL_DAYS) -> bool:
    """Has this account already replied to this target?"""
    return find(account, key(target_type, target_id), ttl_days=ttl_days) is not None


def record(account, store, target_type: str, target_id: str, *,
           url: str = "", instruction_id: str = "") -> None:
    """Remember that this account replied to this target. Never fatal.

    Called on the live-reply success path, in code, so the record exists whether
    or not the agent gets round to writing its memory.
    """
    k = key(target_type, target_id)
    if not k:
        return
    entries = [e for e in _entries(account) if e.get("k") != k]
    entries.append({"k": k, "at": _now().isoformat(), "url": url,
                    "instruction": instruction_id})
    meta = dict(account.meta or {})
    meta[META_KEY] = entries[-MAX_ENTRIES:]
    try:
        account.set_meta(meta)
        store.upsert_account(account)
    except Exception as exc:  # noqa: BLE001 - a sent reply must not fail on bookkeeping
        logger.warning("Could not record the reply fingerprint for %s: %s",
                       account.handle or account.external_id, exc)
        return
    logger.info("Recorded reply to %s for %s%s", k,
                account.handle or account.external_id, f" ({url})" if url else "")


def forget(account, store, target_type: str, target_id: str) -> None:
    """Drop a target — its reply is no longer live (e.g. the comment was deleted)."""
    k = key(target_type, target_id)
    before = _entries(account)
    entries = [e for e in before if e.get("k") != k]
    if len(entries) == len(before):
        return
    meta = dict(account.meta or {})
    meta[META_KEY] = entries
    try:
        account.set_meta(meta)
        store.upsert_account(account)
    except Exception as exc:  # noqa: BLE001 - never block on bookkeeping
        logger.warning("Could not forget answered target %s for %s: %s", k,
                       account.handle or account.external_id, exc)
