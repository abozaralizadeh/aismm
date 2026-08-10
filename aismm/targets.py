"""Parse an instruction's free-text ``engagement_targets`` into typed buckets.

An operator types outreach targets the way they think about them — a mix of
keywords, ``#hashtags``, ``r/subreddits`` and ``@accounts``, separated by commas
or newlines. Each platform's search tool wants a different slice of that (X takes
keywords + hashtags, Reddit takes subreddits + keywords, YouTube takes a query
string), so we normalise the text ONCE here rather than re-parsing it in every
tool. Empty input is valid: an outreach run with no targets infers them from the
brief instead, so this never raises — it just returns empty buckets.

Deterministic and dependency-free, like the rest of the config-parsing layer
(cf. :mod:`aismm.schedules`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Commas and newlines both separate; a phrase keyword may contain spaces, so we do
# NOT split on whitespace. ``;`` also separates, matching the schedule syntax feel.
_SPLIT = re.compile(r"[,\n;]+")
# ``r/foo``, ``/r/foo`` — the leading slash is optional and case-insensitive.
_SUBREDDIT = re.compile(r"^/?r/([A-Za-z0-9_]+)$", re.IGNORECASE)


@dataclass
class Targets:
    """Outreach targets, bucketed by kind. Stored WITHOUT their sigils."""

    keywords: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)      # no leading '#'
    subreddits: list[str] = field(default_factory=list)    # no 'r/' prefix
    accounts: list[str] = field(default_factory=list)      # no leading '@'

    def __bool__(self) -> bool:
        return bool(self.keywords or self.hashtags or self.subreddits or self.accounts)

    def describe(self) -> str:
        """A one-line human/agent-readable summary, or "" when empty."""
        parts = []
        if self.keywords:
            parts.append("keywords: " + ", ".join(self.keywords))
        if self.hashtags:
            parts.append("hashtags: " + ", ".join("#" + h for h in self.hashtags))
        if self.subreddits:
            parts.append("subreddits: " + ", ".join("r/" + s for s in self.subreddits))
        if self.accounts:
            parts.append("accounts: " + ", ".join("@" + a for a in self.accounts))
        return " · ".join(parts)


def _dedupe(items: list[str]) -> list[str]:
    """Order-preserving, case-insensitive de-duplication."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def parse_targets(text: str) -> Targets:
    """``"#ai, r/MachineLearning, @openai, prompt engineering"`` → typed buckets.

    Classification by sigil: ``#`` → hashtag, ``@`` → account, ``r/`` / ``/r/`` →
    subreddit, anything else → a keyword (which may be a multi-word phrase). Each
    bucket is de-duplicated case-insensitively; sigils are stripped so the tools
    get bare terms.
    """
    result = Targets()
    for raw in _SPLIT.split(text or ""):
        token = raw.strip()
        if not token:
            continue
        sub = _SUBREDDIT.match(token)
        if sub:
            result.subreddits.append(sub.group(1))
        elif token.startswith("#") and len(token) > 1:
            result.hashtags.append(token[1:].lstrip("#"))
        elif token.startswith("@") and len(token) > 1:
            result.accounts.append(token[1:].lstrip("@"))
        else:
            result.keywords.append(token)
    result.keywords = _dedupe(result.keywords)
    result.hashtags = _dedupe(result.hashtags)
    result.subreddits = _dedupe(result.subreddits)
    result.accounts = _dedupe(result.accounts)
    return result


def x_query(targets: Targets) -> str:
    """An X recent-search query from keywords + hashtags, OR-joined.

    ``prompt engineering`` becomes a quoted phrase; ``#ai`` a bare hashtag. Returns
    "" when there is nothing to search, so the caller can fall back to the brief.
    """
    terms: list[str] = []
    for kw in targets.keywords:
        terms.append(f'"{kw}"' if " " in kw else kw)
    terms.extend("#" + h for h in targets.hashtags)
    return " OR ".join(terms)


def youtube_query(targets: Targets) -> str:
    """A YouTube search query — space-joined keywords + hashtags (its API ANDs)."""
    terms = list(targets.keywords) + ["#" + h for h in targets.hashtags]
    return " ".join(terms)


def reddit_query(targets: Targets) -> str:
    """A Reddit search query — keywords + hashtag WORDS, OR-joined.

    A ``#`` means nothing to Reddit search, so a hashtag becomes its bare word.
    Multi-word keywords are quoted so the phrase matches. Returns "" when there is
    nothing to search (e.g. the targets are subreddits only — the caller browses
    those directly instead).
    """
    terms: list[str] = []
    for kw in targets.keywords:
        terms.append(f'"{kw}"' if " " in kw else kw)
    terms.extend(targets.hashtags)
    return " OR ".join(terms)
