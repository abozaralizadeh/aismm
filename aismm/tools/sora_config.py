"""Sora 2 pool selection + format constants.

The pool comes from :mod:`aismm.config` (comma-separated, index-aligned endpoints
/ keys / models). Load is spread across resources by round-robin **at the job
level** — a Sora job id only exists on the resource that created it, so a whole
create/poll/download lifecycle must stay pinned to one resource. Never place a
round-robin gateway in front of these endpoints.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from contextvars import ContextVar

from ..config import SoraSettings, settings

logger = logging.getLogger("aismm.sora")

# Sora 2 supports 4 / 8 / 12 second clips and OpenAI-style "WxH" size strings.
ALLOWED_SECONDS = (4, 8, 12)
SIZE_PORTRAIT = "720x1280"    # 9:16 — Reels / TikTok / Shorts
SIZE_LANDSCAPE = "1280x720"   # 16:9 — YouTube / X

_rr_lock = threading.Lock()
_rr_index = 0

# The per-run Sora connection, when an instruction selected one. Set by
# manager_agent for the duration of a run (reset in its finally). It is a
# ContextVar, not module state, because these functions are called DEEP in
# sora_client — below any state-closure tool — and each account run executes in
# its own thread via asyncio.run, so a per-run pool must not leak across runs.
# ``None`` falls back to the deployment ``.env`` pool (settings.sora).
_ACTIVE: ContextVar[SoraSettings | None] = ContextVar("active_sora", default=None)


def _active() -> SoraSettings:
    return _ACTIVE.get() or settings.sora


def api_version() -> str:
    return _active().api_version


def pool() -> list[dict]:
    return [r for r in _active().pool() if r.get("endpoint") and r.get("key")]


def enabled() -> bool:
    return bool(pool())


def job_timeout_seconds() -> float:
    """How long ONE Sora job may run before it is abandoned.

    A ceiling on a single clip, not on the sequence: the caller retries a timed
    out clip on a different resource. Twelve-second clips normally return in
    minutes, so this only fires when a resource has stopped answering.
    """
    return float(settings.sora_job_timeout_seconds or 1800)


def pool_size() -> int:
    return len(pool())


def max_attempts() -> int:
    """How many resources a single clip may try before giving up.

    ``SORA_MAX_ATTEMPTS`` overrides; 0 (the default) means auto — every resource
    in the pool, capped at 3 so one clip can't spend three poll timeouts' worth
    of a run walking a large pool.
    """
    configured = _active().max_attempts
    if configured > 0:
        return min(configured, max(pool_size(), 1))
    return max(min(pool_size(), 3), 1)


# --- pool health, remembered ACROSS runs --------------------------------------- #
# The point of the pool is to route around a resource that is down — an expired
# subscription, a key rotated out, a model never deployed. That only works if the
# knowledge outlives the clip that bought it. It used to die with the caller: a
# per-sequence `unusable` dict, discarded at the end of the run. So every run
# started blind, the round-robin handed out the same dead resource first, and
# every run paid the same 401 before finding the one that works (observed: run
# 5afab777, veronica 401 then gioak, on a pool where veronica had been dead for
# days).
#
# Keyed by endpoint AND a fingerprint of the key, because the two permanent
# failures are fixed in opposite ways: "no deployment" is fixed on the endpoint,
# a rejected key is fixed by supplying a DIFFERENT key for that same endpoint.
# Keying on the endpoint alone would make a freshly-rotated key inherit the old
# key's verdict and stay skipped until the TTL expired.
_health: dict[str, dict] = {}
_health_lock = threading.Lock()


def _health_key(resource: dict) -> str:
    secret = (resource.get("key") or "").encode()
    return f"{resource.get('endpoint', '')}#{hashlib.sha256(secret).hexdigest()[:12]}"


def mark_unusable(resource: dict, reason: str) -> None:
    """Record that this resource cannot serve Sora, for ``health_ttl_seconds``."""
    if _active().health_ttl_seconds <= 0:
        return
    with _health_lock:
        _health[_health_key(resource)] = {"ok": False, "reason": reason, "at": time.time()}


def mark_healthy(resource: dict) -> None:
    """Record that this resource just served a clip — clears any old verdict."""
    with _health_lock:
        _health[_health_key(resource)] = {"ok": True, "reason": "", "at": time.time()}


def unusable_reason(resource: dict) -> str:
    """Why this resource is currently skipped, or ``""`` if it is fair game.

    A verdict older than the TTL is forgotten rather than trusted: the fix for
    both permanent failures happens in Azure, where this process cannot see it,
    so the pool has to re-try a dead member eventually or it can never heal.
    """
    ttl = _active().health_ttl_seconds
    if ttl <= 0:
        return ""
    with _health_lock:
        entry = _health.get(_health_key(resource))
        if not entry or entry["ok"]:
            return ""
        if time.time() - entry["at"] >= ttl:
            _health.pop(_health_key(resource), None)
            return ""
        return entry["reason"]


def known_unusable() -> dict[str, str]:
    """``{endpoint: reason}`` for every resource currently marked bad."""
    return {r["endpoint"]: reason for r in pool() if (reason := unusable_reason(r))}


def reset_health() -> None:
    """Forget every verdict (tests, and an operator who has just fixed the pool)."""
    with _health_lock:
        _health.clear()


def next_resource(exclude_endpoints: set[str] | None = None) -> dict:
    """Round-robin pick of a usable ``{endpoint, key, model}`` resource.

    Two filters, both of which step aside rather than empty the pool — asking a
    known-bad resource beats raising "nothing to try":

    * ``exclude_endpoints`` — already failed THIS clip, so a retry lands elsewhere;
    * resources currently marked unusable (see :func:`unusable_reason`).

    Round-robin still spreads load, but only across the members that can actually
    serve: balancing over a dead resource is not balancing, it is a wasted call
    on every other clip.
    """
    usable = pool()
    if not usable:
        raise RuntimeError(
            "No Sora resources configured. Set AZURE_OPENAI_ENDPOINT_SORA / "
            "AZURE_OPENAI_API_KEY_SORA (comma-separated for multiple resources)."
        )
    if exclude_endpoints:
        remaining = [r for r in usable if r["endpoint"] not in exclude_endpoints]
        if remaining:
            usable = remaining
    healthy = [r for r in usable if not unusable_reason(r)]
    if healthy:
        usable = healthy
    global _rr_index
    with _rr_lock:
        resource = usable[_rr_index % len(usable)]
        _rr_index += 1
    return resource


def normalize_seconds(seconds: int) -> int:
    """Snap a requested duration to the nearest allowed Sora clip length."""
    return min(ALLOWED_SECONDS, key=lambda s: abs(s - int(seconds)))
