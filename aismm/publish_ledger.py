"""What this account has already published — the duplicate guard.

Two live posts of the same comic panel went out because the ONE fact a run must
never lose — "this content was published" — was recorded by the model, in prose,
optionally. The agent wrote its memory *before* calling publish ("attempting
panel X"), the publish succeeded, and it never wrote the outcome afterwards. The
next scheduled run read "attempting panel X", concluded X was still outstanding,
and posted it again.

The prompt already tells the agent to record the outcome after publish returns
(steps 8/9/10) and it did not. That is the same lesson as the AI disclosure and
the publish-mode gate: a guarantee that must hold on every path belongs in code,
beside the gate, not in the instructions. So every successful publish is
fingerprinted here, and a live publish of a fingerprint already in the ledger is
REFUSED rather than duplicated.

The fingerprint is the sha256 of the media bytes plus the placement — content
identity, not caption identity. The same panel fetched twice produces
byte-identical files (that is how the duplicate was diagnosed: 3054424-byte PNG →
367815-byte JPEG in both runs), while a genuinely new post differs. A caption is
the wrong key: the agent rewrites it each run, so identical media would slip
through.

Stored in the account's ``meta`` like the [cooldown](cooldown.py) deadline — no
new table, no new Store methods, and it round-trips on both storage backends.
Bounded to the most recent ``MAX_ENTRIES`` posts, so ``meta_json`` cannot grow
without limit (Azure Table caps a property at 64KB).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("aismm.publish_ledger")

META_KEY = "published_fingerprints"

# Enough history to catch a re-post across a few scheduled runs, small enough to
# stay well inside the account meta's size budget. One entry PER ITEM now, so a
# run of carousels eats these faster than one-per-post did.
MAX_ENTRIES = 120
# How long a fingerprint blocks a re-post. A legitimate re-post of the same media
# (a "best of" repeat) is rare; an accidental duplicate happens within hours.
DEFAULT_TTL_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fingerprints(asset_paths: list[str], placement: str = "feed") -> list[str]:
    """Content identity **per item**: each file's bytes + where the post goes.

    ONE FINGERPRINT PER ITEM, not one per post. Hashing every item into a single
    combined digest is what let a duplicate through: a panel published on its own
    and then published again as item 1 of a two-photo carousel produced two
    different combined digests, so the guard never fired and the same panel went
    out twice. An item is the unit a follower actually sees repeated.

    Placement is part of each item's identity, so the same art may legitimately go
    out once as a feed post and once as a story.

    Unreadable assets are skipped rather than blocking the post — a missing file is
    the business of the media-presence checks in ``perform_publish``.
    """
    from .assets import read_bytes

    prefix = f"{(placement or 'feed').lower()}\n".encode()
    digests: list[str] = []
    for path in asset_paths or []:
        try:
            data = read_bytes(path)
        except Exception as exc:  # noqa: BLE001 - never block a post on the guard
            logger.warning("Could not fingerprint %s (%s); skipping the duplicate guard "
                           "for this asset", path, exc)
            continue
        digests.append(hashlib.sha256(prefix + hashlib.sha256(data).digest()).hexdigest())
    return digests


def fingerprint(asset_paths: list[str], placement: str = "feed") -> str:
    """First item's fingerprint, for callers that publish exactly one asset."""
    digests = fingerprints(asset_paths, placement)
    return digests[0] if digests else ""


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


def find(account, digest: str, *, ttl_days: int = DEFAULT_TTL_DAYS) -> dict | None:
    """The ledger entry for this fingerprint, if this account posted it recently."""
    if not digest:
        return None
    cutoff = _now() - timedelta(days=ttl_days)
    for entry in _entries(account):
        if entry.get("fp") != digest:
            continue
        stamp = _parse(entry.get("at"))
        if stamp is None or stamp >= cutoff:
            return entry
    return None


def find_any(account, digests: list[str], *,
             ttl_days: int = DEFAULT_TTL_DAYS) -> tuple[int, dict] | None:
    """First already-published ITEM in this post, as ``(index, entry)``.

    A carousel is a duplicate if ANY of its items was already published — that is
    the case the old per-post fingerprint missed.
    """
    for index, digest in enumerate(digests or []):
        entry = find(account, digest, ttl_days=ttl_days)
        if entry is not None:
            return index, entry
    return None


def record(account, store, digest, *, url: str = "", external_id: str = "",
           instruction_id: str = "") -> None:
    """Remember that this account published this content. Never fatal.

    ``digest`` may be one fingerprint or a list of them (one per carousel item);
    every item is recorded, so a later post reusing any single one is caught.

    Called on the live-publish success path, in code, so the record exists whether
    or not the agent gets round to writing its memory.
    """
    digests = [d for d in ([digest] if isinstance(digest, str) else list(digest or [])) if d]
    if not digests:
        return
    entries = [e for e in _entries(account) if e.get("fp") not in set(digests)]
    stamp = _now().isoformat()
    entries.extend({"fp": d, "at": stamp, "url": url,
                    "id": external_id, "instruction": instruction_id} for d in digests)
    meta = dict(account.meta or {})
    meta[META_KEY] = entries[-MAX_ENTRIES:]
    try:
        account.set_meta(meta)
        store.upsert_account(account)
    except Exception as exc:  # noqa: BLE001 - a published post must not fail on bookkeeping
        logger.warning("Could not record the publish fingerprint for %s: %s",
                       account.handle or account.external_id, exc)
        return
    logger.info("Recorded %d publish fingerprint(s) %s for %s%s", len(digests),
                ", ".join(d[:12] for d in digests),
                account.handle or account.external_id, f" ({url})" if url else "")


def forget(account, store, digest: str) -> None:
    """Drop a fingerprint — the post it recorded is no longer on the account.

    The ledger is a record of what we posted, not of what is *currently live*. A
    human deleting a post by hand must be able to have it published again, so a
    refusal that turns out to reference a deleted post removes the entry instead
    of blocking that content forever.
    """
    entries = [e for e in _entries(account) if e.get("fp") != digest]
    if len(entries) == len(_entries(account)):
        return
    meta = dict(account.meta or {})
    meta[META_KEY] = entries
    try:
        account.set_meta(meta)
        store.upsert_account(account)
    except Exception as exc:  # noqa: BLE001 - never block a publish on bookkeeping
        logger.warning("Could not forget fingerprint %s for %s: %s", digest[:12],
                       account.handle or account.external_id, exc)
        return
    logger.info("Forgot publish fingerprint %s for %s — the post is no longer on the "
                "account", digest[:12], account.handle or account.external_id)


def describe_entry(entry: dict) -> str:
    """Human-readable 'when and where' for a duplicate refusal message."""
    stamp = _parse(entry.get("at"))
    when = stamp.strftime("%Y-%m-%d %H:%M UTC") if stamp else "earlier"
    return f"{when}{f' — {entry['url']}' if entry.get('url') else ''}"
