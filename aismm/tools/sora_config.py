"""Sora 2 pool selection + format constants.

The pool comes from :mod:`aismm.config` (comma-separated, index-aligned endpoints
/ keys / models). Load is spread across resources by round-robin **at the job
level** — a Sora job id only exists on the resource that created it, so a whole
create/poll/download lifecycle must stay pinned to one resource. Never place a
round-robin gateway in front of these endpoints.
"""
from __future__ import annotations

import threading
from contextvars import ContextVar

from ..config import SoraSettings, settings

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


def next_resource(exclude_endpoints: set[str] | None = None) -> dict:
    """Round-robin pick of a usable ``{endpoint, key, model}`` resource.

    ``exclude_endpoints`` skips resources that already failed this clip so a retry
    lands elsewhere — ignored if it would empty the pool.
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
    global _rr_index
    with _rr_lock:
        resource = usable[_rr_index % len(usable)]
        _rr_index += 1
    return resource


def normalize_seconds(seconds: int) -> int:
    """Snap a requested duration to the nearest allowed Sora clip length."""
    return min(ALLOWED_SECONDS, key=lambda s: abs(s - int(seconds)))
