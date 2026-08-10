"""Turn a run's raw metrics dict into ordered, human-labelled pills for the UI.

The sibling of :func:`aismm.agent.prompts._format_metrics` (which renders one text
line for the agent). Here the dashboard needs the pieces separately so each counter
can be its own pill, so this returns ``[{"label": ..., "value": ...}, …]``.

The keys differ per platform (X has impressions/reposts, Reddit a score and an
upvote_ratio, YouTube views) and new ones may appear, so the formatter stays
generic: known keys get a friendly label and a fixed display order, unknown keys
fall through with their raw name rather than being dropped.
"""
from __future__ import annotations

# Friendly labels for the counters the platforms record today. An unknown key is
# shown with its underscores turned to spaces rather than hidden — a metric we
# forgot to name here is still worth seeing.
_LABELS = {
    "views": "views",
    "impressions": "impressions",
    "reach": "reach",
    "likes": "likes",
    "score": "score",
    "reposts": "reposts",
    "shares": "shares",
    "quotes": "quotes",
    "replies": "replies",
    "comments": "comments",
    "saves": "saves",
    "upvote_ratio": "upvote ratio",
}

# Most-reach-first, so the eye lands on the headline number. Keys not listed here
# sort after these, alphabetically, so the order is stable across runs.
_ORDER = ["views", "impressions", "reach", "likes", "score", "reposts",
          "shares", "quotes", "replies", "comments", "saves", "upvote_ratio"]


def _format_value(key: str, value) -> str | None:
    """One counter as display text, or ``None`` to drop it (bools carry no count)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        # A 0–1 float is a ratio (upvote_ratio) — a percentage reads better than
        # "0.95". Anything larger keeps two decimals.
        if key.endswith("ratio") or 0.0 <= value <= 1.0:
            return f"{value:.0%}"
        return f"{value:,.2f}"
    return str(value)


def format_metrics(metrics: dict) -> list[dict]:
    """``{'views': 1200, 'likes': 85}`` → ``[{'label': 'views', 'value': '1,200'}, …]``.

    Ordered by :data:`_ORDER` then alphabetically for the rest, so the same post
    always renders its counters in the same sequence.
    """
    if not metrics:
        return []

    def _rank(key: str) -> tuple[int, str]:
        return (_ORDER.index(key) if key in _ORDER else len(_ORDER), key)

    pills = []
    for key in sorted(metrics, key=_rank):
        text = _format_value(key, metrics[key])
        if text is None:
            continue
        pills.append({"label": _LABELS.get(key, key.replace("_", " ")), "value": text})
    return pills
