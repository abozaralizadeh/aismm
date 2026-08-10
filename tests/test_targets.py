"""Unit tests for aismm.targets — the outreach free-text parser.

Deterministic and dependency-free (no network, no store, no LLM); it just turns
what an operator typed into typed buckets and per-platform search strings.
"""
from __future__ import annotations

from aismm.targets import (
    Targets, parse_targets, reddit_query, x_query, youtube_query,
)


# --- parse_targets: classification by sigil ------------------------------------------ #

def test_parse_classifies_each_sigil():
    t = parse_targets("prompt engineering, #AI, r/MachineLearning, @openai")
    assert t.keywords == ["prompt engineering"]
    assert t.hashtags == ["AI"]                # '#' stripped
    assert t.subreddits == ["MachineLearning"]  # 'r/' stripped
    assert t.accounts == ["openai"]            # '@' stripped


def test_parse_accepts_newlines_and_semicolons_as_separators():
    t = parse_targets("one\ntwo; three, four")
    assert t.keywords == ["one", "two", "three", "four"]


def test_parse_keeps_multiword_keyword_as_one_phrase():
    t = parse_targets("large language models")
    assert t.keywords == ["large language models"]


def test_parse_subreddit_leading_slash_and_case_insensitive():
    t = parse_targets("/r/LocalLLaMA")
    assert t.subreddits == ["LocalLLaMA"]


def test_parse_dedupes_case_insensitively_preserving_first():
    t = parse_targets("#AI, #ai, #Ai")
    assert t.hashtags == ["AI"]


def test_parse_strips_repeated_sigils():
    t = parse_targets("##AI, @@openai")
    assert t.hashtags == ["AI"]
    assert t.accounts == ["openai"]


def test_empty_input_is_valid_and_falsy():
    t = parse_targets("")
    assert not t
    assert t == Targets()
    assert parse_targets("   ,  \n ; ") == Targets()


def test_targets_is_truthy_when_any_bucket_has_content():
    assert bool(parse_targets("just a keyword"))
    assert bool(parse_targets("#tag"))
    assert bool(parse_targets("r/sub"))
    assert bool(parse_targets("@acct"))


def test_describe_summarises_every_bucket_with_sigils():
    t = parse_targets("prompt engineering, #AI, r/MachineLearning, @openai")
    desc = t.describe()
    assert "keywords: prompt engineering" in desc
    assert "#AI" in desc
    assert "r/MachineLearning" in desc
    assert "@openai" in desc
    assert Targets().describe() == ""


# --- per-platform query strings ------------------------------------------------------ #

def test_x_query_quotes_phrases_keeps_hashes_or_joins():
    q = x_query(parse_targets("prompt engineering, ai, #LLM"))
    assert q == '"prompt engineering" OR ai OR #LLM'


def test_x_query_empty_when_only_subreddits_or_accounts():
    assert x_query(parse_targets("r/MachineLearning, @openai")) == ""


def test_youtube_query_space_joins_keywords_and_hashtags():
    q = youtube_query(parse_targets("prompt engineering, #AI"))
    assert q == "prompt engineering #AI"


def test_reddit_query_drops_the_hash_and_quotes_phrases():
    q = reddit_query(parse_targets("prompt engineering, #AI"))
    assert q == '"prompt engineering" OR AI'


def test_reddit_query_empty_when_only_subreddits():
    # subreddits are browsed directly, so a subreddit-only target has no keyword query
    assert reddit_query(parse_targets("r/MachineLearning, r/LocalLLaMA")) == ""
