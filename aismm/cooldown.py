"""Per-account publishing cooldowns.

When a platform refuses an action for *volume* reasons — Instagram's code 4
"application request limit reached", or the same thing dressed up as "action is
blocked — we restrict certain activity to protect our community" — trying again
soon makes it worse. Meta extends these blocks when an app keeps knocking.

A schedule of "every 1h" against one Instagram account is 24 posts a day, which
is what triggers it. So after a volume refusal the account is put in a cooldown,
and the orchestrator **skips the run before it starts** rather than browsing,
downloading, converting and generating media only to be refused at the last step.

**A flat 60-minute cooldown against an hourly schedule is close to useless**: if
whatever actually triggered the block (an integrity throttle, not the documented
25-posts/24h quota) lasts longer than an hour, the very next scheduled run knocks
again the moment the cooldown clears — and by our own reasoning above, that knock
makes Meta extend the real block further. So repeated refusals **escalate**: each
one doubles the cooldown from the base (1h -> 2h -> 4h -> …), capped at
``MAX_COOLDOWN_SECONDS`` (24h) so a stuck account isn't silently dead forever. A
clean, non-reconciled publish resets the streak — the point is to back off harder
while the block is actually recurring, not to punish an account permanently for
one bad hour.

Both the deadline and the strike count live in the account's ``meta`` — no new
table and no new Store methods, and they survive on both storage backends
because ``meta_json`` already round-trips.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("aismm.cooldown")

META_KEY = "publish_blocked_until"
STRIKES_KEY = "publish_blocked_strikes"
MAX_COOLDOWN_SECONDS = 24 * 3600


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


def deadline(account) -> datetime | None:
    """When this account may publish again, or ``None`` when it is not blocked."""
    parsed = _parse((account.meta or {}).get(META_KEY))
    return parsed if parsed and parsed > _now() else None


def remaining_seconds(account) -> int:
    """How long this account is still blocked from publishing. 0 when clear."""
    ends = _parse((account.meta or {}).get(META_KEY))
    if ends is None:
        return 0
    return max(int((ends - _now()).total_seconds()), 0)


def is_active(account) -> bool:
    return remaining_seconds(account) > 0


def strike_count(account) -> int:
    """How many rate limits this account has hit in the current streak."""
    try:
        return max(int((account.meta or {}).get(STRIKES_KEY, 0)), 0)
    except (TypeError, ValueError):
        return 0


def start(account, store, seconds: int, *, reason: str = "") -> int:
    """Block publishing for this account. Returns the seconds actually set.

    ``seconds`` is the BASE cooldown for a first offense; each call doubles it
    from there (capped at ``MAX_COOLDOWN_SECONDS``), because a refusal that keeps
    recurring at the base duration means the schedule is knocking on a block that
    hasn't actually lifted. Also extends rather than shortens an already-longer
    cooldown, so a shorter reconciled-publish cooldown can't cut a longer one short.
    """
    strikes = strike_count(account) + 1
    escalated = min(int(seconds) * (2 ** (strikes - 1)), MAX_COOLDOWN_SECONDS)
    current = remaining_seconds(account)
    held = max(escalated, current)

    meta = dict(account.meta or {})
    meta[META_KEY] = (_now() + timedelta(seconds=held)).isoformat()
    meta[STRIKES_KEY] = strikes
    account.set_meta(meta)
    store.upsert_account(account)
    logger.warning("Publishing cooldown for %s (%s): %d minutes (strike %d)%s",
                   account.handle or account.external_id, account.platform.value,
                   held // 60, strikes, f" — {reason}" if reason else "")
    return held


def clear(account, store) -> None:
    """Lift the cooldown and reset the strike streak (a clean publish got through)."""
    meta = dict(account.meta or {})
    had_either = meta.pop(META_KEY, None) is not None or meta.pop(STRIKES_KEY, None) is not None
    if had_either:
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
