"""What an X run is CHARGED for: requests made, and post objects returned.

X has no free tier (since Feb 2026) and bills a read twice over — once as a
request, and again for every post object it hands back. Five euro of credit went
in a couple of days, and the account analytics showed where: a daily metrics sweep
asking about every recent post one at a time, and engagement reads repeated inside
a single run.

These tests pin the two fixes, both of which are pure waste removal — the agent
sees exactly the same information either way.
"""
from __future__ import annotations

import asyncio

import pytest
from agents import RunConfig
from agents.tool_context import ToolContext

from aismm.models import Account, Instruction, InstructionTask, PlatformName
from aismm.platforms import registry
from aismm.tools import twitter_tools


def _account():
    return Account(platform=PlatformName.twitter, handle="me", external_id="9")


def _invoke(tool, arguments: str = "{}"):
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="1",
                      tool_arguments=arguments, run_config=RunConfig())
    return asyncio.run(tool.on_invoke_tool(ctx, arguments))


@pytest.fixture()
def wired(monkeypatch, store):
    """A run state whose X platform counts every read it is asked for."""
    account = _account()
    state = {"account": account, "run": None, "store": store,
             "instruction": Instruction(name="e", brief="b",
                                        task_type=InstructionTask.engage)}

    class _Platform:
        def __init__(self):
            self.calls: list[str] = []

        async def list_posts(self, token, acct, *, limit=10):
            self.calls.append("posts")
            return [{"id": "P1", "text": "ours"}]

        async def list_replies(self, token, acct, *, limit=10):
            self.calls.append("replies")
            return [{"id": "r1", "text": "nice one", "author_id": "111"}]

        async def list_mentions(self, token, acct, *, limit=10):
            self.calls.append("mentions")
            return [{"id": "m1", "text": "hey @me", "author_id": "222"}]

        async def search_content(self, token, acct, *, query, limit=10, subreddit=""):
            self.calls.append(f"search:{query}")
            return [{"id": "s1", "text": "a stranger's post"}]

        async def list_dms(self, token, acct, *, limit=25):
            self.calls.append("dms")
            return [{"id": "d1", "conversation_id": "c1", "text": "hello"}]

    platform = _Platform()

    async def context(_state):
        return platform, account, "token"

    monkeypatch.setattr(twitter_tools, "_context", context)
    return state, platform, account


# --- reading the same thing twice in one run ----------------------------------------- #
# A model that reads its replies, answers two and then reads them again "to check"
# paid full price for the second look at a list nobody had added to.

def test_reading_replies_twice_in_one_run_costs_one_call(wired):
    state, platform, _account = wired
    tool = twitter_tools._make_replies(state)
    _invoke(tool, '{"limit": 10}')
    _invoke(tool, '{"limit": 10}')
    assert platform.calls == ["replies"]


@pytest.mark.parametrize("factory,args,expected", [
    ("_make_recent_posts", '{"limit": 10}', "posts"),
    ("_make_mentions", '{"limit": 10}', "mentions"),
    ("_make_dms", '{"limit": 25}', "dms"),
])
def test_every_read_tool_asks_x_once_per_run(wired, factory, args, expected):
    state, platform, _account = wired
    tool = getattr(twitter_tools, factory)(state)
    _invoke(tool, args)
    _invoke(tool, args)
    assert platform.calls == [expected]


def test_a_different_query_is_a_different_read(wired):
    """The cache is keyed on the ARGUMENTS — asking something else must still ask."""
    state, platform, _account = wired
    tool = twitter_tools._make_search(state)
    _invoke(tool, '{"query": "robots", "limit": 10}')
    _invoke(tool, '{"query": "puppies", "limit": 10}')
    _invoke(tool, '{"query": "robots", "limit": 10}')
    assert platform.calls == ["search:robots", "search:puppies"]


def test_a_failed_read_is_not_cached_as_an_answer(wired, monkeypatch):
    """An error is not a result: a rate-limited read must be retryable in the run."""
    state, platform, _account = wired
    attempts = {"n": 0}

    async def flaky(token, acct, *, limit=10):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("X API 429: rate limit")
        return [{"id": "m1", "text": "hey", "author_id": "222"}]

    monkeypatch.setattr(platform, "list_mentions", flaky)
    tool = twitter_tools._make_mentions(state)
    first = _invoke(tool, '{"limit": 10}')
    second = _invoke(tool, '{"limit": 10}')
    assert "429" in str(first) and "m1" in str(second)


def test_a_cached_list_still_shows_what_was_just_answered(wired):
    """Only the raw items are cached; ``already_answered`` is recomputed each call.

    Caching the rendered view would tell the agent a comment is still open after
    it had replied to it — which is how a run answers the same person twice.
    """
    from aismm import engagement_ledger

    state, _platform, account = wired
    tool = twitter_tools._make_replies(state)
    assert "'already_answered': False" in str(_invoke(tool, '{"limit": 10}'))
    engagement_ledger.record(account, state["store"], "tweet", "r1")
    assert "'already_answered': True" in str(_invoke(tool, '{"limit": 10}'))


# --- polling metrics for many posts -------------------------------------------------- #
# GET /2/tweets takes 100 ids. The sweep was asking one at a time, every morning,
# for every post inside METRICS_REFRESH_DAYS — a request per post per day, growing
# with the account's history.

def test_x_reads_many_posts_metrics_in_one_request(monkeypatch):
    from aismm.platforms import twitter as tw

    seen: list[dict] = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"id": "1", "public_metrics": {"like_count": 4}},
                             {"id": "2", "public_metrics": {"like_count": 7}}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            seen.append({"url": url, "params": kw.get("params", {})})
            return _Resp()

    monkeypatch.setattr(tw.httpx, "AsyncClient", lambda **kw: _Client())
    got = asyncio.run(registry.get_platform(PlatformName.twitter).fetch_post_metrics_bulk(
        "t", _account(), external_ids=["1", "2"]))
    assert len(seen) == 1 and seen[0]["params"]["ids"] == "1,2"
    assert got["1"]["likes"] == 4 and got["2"]["likes"] == 7


def test_more_than_a_hundred_posts_are_split_into_chunks(monkeypatch):
    """X caps the lookup at 100 ids, so a long history is chunked, not truncated."""
    from aismm.platforms import twitter as tw

    seen: list[list[str]] = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": []}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            seen.append(kw["params"]["ids"].split(","))
            return _Resp()

    monkeypatch.setattr(tw.httpx, "AsyncClient", lambda **kw: _Client())
    asyncio.run(registry.get_platform(PlatformName.twitter).fetch_post_metrics_bulk(
        "t", _account(), external_ids=[str(i) for i in range(150)]))
    assert [len(chunk) for chunk in seen] == [100, 50]


def test_one_unreadable_chunk_does_not_stop_the_sweep(monkeypatch):
    """A deleted post makes the lookup partial, never fatal."""
    from aismm.platforms import twitter as tw

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            raise RuntimeError("X API 503: service unavailable")

    monkeypatch.setattr(tw.httpx, "AsyncClient", lambda **kw: _Client())
    got = asyncio.run(registry.get_platform(PlatformName.twitter).fetch_post_metrics_bulk(
        "t", _account(), external_ids=["1"]))
    assert got == {}
