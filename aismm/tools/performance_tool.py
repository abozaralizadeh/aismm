"""``recent_performance`` — how THIS account's recent posts performed.

The pull half of the performance feedback loop. The kickoff already inlines a
summary (so a scheduled run sees it from turn one — see
:func:`aismm.agent.prompts.build_performance_block`), but the agent can also ask
for the detail here, e.g. before deciding what to post next.

Deterministic, like every tool: it reads counters the daily metrics sweep
(:func:`aismm.orchestrator.refresh_metrics`) already stored on each ``Run`` and
makes NO platform call itself.
"""
from __future__ import annotations

from agents import function_tool

from ..models import RunStatus
from .registry import register_tool

# How many recent posts (that carry polled metrics) to surface.
_LIMIT = 5


def recent_performance_runs(store, account_id: str, *, limit: int = _LIMIT) -> list:
    """The account's most recent published runs that carry polled metrics.

    Shared by the kickoff builder and the tool so both show the same posts.
    Over-fetches then filters: a run only has metrics once the sweep has polled
    it, so recent-but-unpolled posts are skipped rather than shown empty.
    """
    runs = store.list_runs(account_id=account_id, status=RunStatus.published,
                           limit=max(limit * 4, 20), sort="created_at", descending=True)
    with_metrics = [r for r in runs if r.metrics]
    return with_metrics[:limit]


def _make_recent_performance(state: dict):
    @function_tool
    async def recent_performance() -> dict:
        """How THIS account's recent posts performed (likes / views / comments / …).

        Read this to learn what worked before choosing what to post: lean into the
        angles and formats that got traction. Counts are approximate and refreshed
        about once a day, so a just-published post may show none yet. Returns an
        empty ``posts`` list when nothing has metrics recorded.
        """
        account = state["account"]
        store = state["store"]
        runs = recent_performance_runs(store, account.id)
        return {
            "account": account.handle or account.external_id,
            "platform": account.platform.value,
            "posts": [
                {
                    "at": (r.created_at.strftime("%Y-%m-%d")
                           if getattr(r, "created_at", None) else ""),
                    "url": r.external_url,
                    "caption": " ".join((r.caption or "").split())[:120],
                    "metrics": r.metrics,
                }
                for r in runs
            ],
            "note": ("Metrics are refreshed about once a day; a just-published post "
                     "may not have any yet."),
        }

    return recent_performance


register_tool("recent_performance", _make_recent_performance)
