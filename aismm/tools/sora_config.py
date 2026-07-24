"""Sora 2 pool selection + format constants.

The pool comes from :mod:`aismm.config` (comma-separated, index-aligned endpoints
/ keys / models). Load is spread across resources by round-robin **at the job
level** — a Sora job id only exists on the resource that created it, so a whole
create/poll/download lifecycle must stay pinned to one resource. Never place a
round-robin gateway in front of these endpoints.
"""
from __future__ import annotations

import threading

from ..config import settings

# Sora 2 supports 4 / 8 / 12 second clips and OpenAI-style "WxH" size strings.
ALLOWED_SECONDS = (4, 8, 12)
SIZE_PORTRAIT = "720x1280"    # 9:16 — Reels / TikTok / Shorts
SIZE_LANDSCAPE = "1280x720"   # 16:9 — YouTube / X

_rr_lock = threading.Lock()
_rr_index = 0


def api_version() -> str:
    return settings.sora.api_version


def pool() -> list[dict]:
    return [r for r in settings.sora.pool() if r.get("endpoint") and r.get("key")]


def enabled() -> bool:
    return bool(pool())


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
