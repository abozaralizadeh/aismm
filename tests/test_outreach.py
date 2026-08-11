"""Outreach: find OTHER people's posts on X and Reddit and engage them.

Covers the search-content platform methods (request shape + normalisation), the
outreach search/reply tools (target fallback, already-answered annotation, the
mode gate), and the outreach prompt/registry wiring. Network is mocked
throughout; nothing here spends a credit or an API call.
"""
import asyncio
import json

import httpx
import pytest

from aismm.models import (
    Account, Instruction, InstructionTask, PlatformName,
)
from aismm.platforms.registry import get_platform
from aismm.tools.registry import build_tools


# --- capabilities + the base contract ------------------------------------------------ #

def test_only_x_and_reddit_declare_search():
    searchable = {n.value for n in PlatformName
                  if getattr(get_platform(n).capabilities, "supports_search", False)}
    assert searchable == {"twitter", "reddit"}


@pytest.mark.parametrize("name", [PlatformName.youtube, PlatformName.tiktok,
                                  PlatformName.instagram])
def test_a_platform_without_search_raises_the_base_method(name):
    """A non-searching platform inherits the base method, which refuses — so a
    stray search call fails loudly instead of pretending to return results."""
    platform = get_platform(name)
    assert platform.capabilities.supports_search is False
    account = Account(platform=name, external_id="1")
    with pytest.raises(RuntimeError):
        asyncio.run(platform.search_content("t", account, query="x"))


# --- X.search_content ---------------------------------------------------------------- #

def _x():
    return get_platform(PlatformName.twitter)


def _patch_x_get(monkeypatch, payload):
    """Capture the params X search sends and return a canned payload."""
    from aismm.platforms import twitter as tw

    seen = {}

    async def fake_get(self, access_token, path, params):
        seen["path"] = path
        seen["params"] = params
        return payload

    monkeypatch.setattr(tw.Twitter, "_get", fake_get)
    return seen


def test_x_search_narrows_to_original_posts_and_excludes_self(monkeypatch):
    seen = _patch_x_get(monkeypatch, {
        "data": [
            {"id": "1", "text": "a great take", "author_id": "111",
             "public_metrics": {"like_count": 4, "retweet_count": 1, "reply_count": 2}},
            {"id": "2", "text": "my own post", "author_id": "9"},  # us — dropped
        ],
        "includes": {"users": [{"id": "111", "username": "someone"}]},
    })
    account = Account(platform=PlatformName.twitter, handle="me", external_id="9")
    items = asyncio.run(_x().search_content("t", account, query="ai agents", limit=10))

    assert seen["path"] == "tweets/search/recent"
    q = seen["params"]["query"]
    assert "(ai agents)" in q
    assert "-is:retweet" in q and "-is:reply" in q
    assert "-from:me" in q
    # Only the stranger's post survives, normalised with an @handle and counts.
    assert [i["id"] for i in items] == ["1"]
    assert items[0]["author"] == "@someone"
    assert items[0]["url"] == "https://x.com/someone/status/1"
    assert items[0]["likes"] == 4


def test_x_search_with_no_query_makes_no_call(monkeypatch):
    called = {"n": 0}

    async def fake_get(self, *a, **kw):
        called["n"] += 1
        return {}

    from aismm.platforms import twitter as tw
    monkeypatch.setattr(tw.Twitter, "_get", fake_get)
    account = Account(platform=PlatformName.twitter, handle="me", external_id="9")
    assert asyncio.run(_x().search_content("t", account, query="  ")) == []
    assert called["n"] == 0


def test_x_search_is_best_effort_on_failure(monkeypatch):
    from aismm.platforms import twitter as tw

    async def boom(self, *a, **kw):
        raise RuntimeError("402 no credits")

    monkeypatch.setattr(tw.Twitter, "_get", boom)
    account = Account(platform=PlatformName.twitter, handle="me", external_id="9")
    assert asyncio.run(_x().search_content("t", account, query="x")) == []


# --- Reddit.search_content ----------------------------------------------------------- #

def _rd():
    return get_platform(PlatformName.reddit)


def _reddit_transport(monkeypatch, handler):
    """Route Reddit's httpx client through a MockTransport; return recorded requests."""
    from aismm.platforms import reddit as rd

    requests: list[httpx.Request] = []

    def record(request):
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    real = httpx.AsyncClient
    monkeypatch.setattr(rd.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    return requests


def _listing(children):
    return httpx.Response(200, json={"data": {"children": children}})


def _t3(id_, **data):
    return {"kind": "t3", "data": {"name": f"t3_{id_}", "id": id_, **data}}


def test_reddit_search_in_a_subreddit_restricts_and_normalises(monkeypatch):
    def handler(request):
        return _listing([
            _t3("aaa", title="Great question", selftext="body here",
                author="someone", subreddit="MachineLearning",
                permalink="/r/MachineLearning/comments/aaa/x/", score=12,
                num_comments=3, created_utc=1_700_000_000.0),
            _t3("nsfw", title="nope", over_18=True, author="x"),   # dropped
        ])

    requests = _reddit_transport(monkeypatch, handler)
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    items = asyncio.run(_rd().search_content(
        "t", account, query="agents", subreddit="r/MachineLearning", limit=10))

    assert "/r/MachineLearning/search" in str(requests[0].url)
    assert "restrict_sr=1" in str(requests[0].url)
    assert [i["id"] for i in items] == ["t3_aaa"]           # t3_ fullname, NSFW dropped
    assert items[0]["author"] == "u/someone"
    assert items[0]["url"] == "https://www.reddit.com/r/MachineLearning/comments/aaa/x/"
    assert items[0]["text"].startswith("Great question")


def test_reddit_subreddit_only_browses_new(monkeypatch):
    requests = _reddit_transport(monkeypatch, lambda r: _listing([]))
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    asyncio.run(_rd().search_content("t", account, query="", subreddit="LocalLLaMA"))
    assert "/r/LocalLLaMA/new" in str(requests[0].url)


def test_reddit_query_only_searches_site_wide_links(monkeypatch):
    requests = _reddit_transport(monkeypatch, lambda r: _listing([]))
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    asyncio.run(_rd().search_content("t", account, query="prompt engineering"))
    url = str(requests[0].url)
    assert "/search" in url and "type=link" in url
    assert "restrict_sr" not in url


def test_reddit_search_drops_our_own_posts(monkeypatch):
    def handler(request):
        return _listing([_t3("mine", title="ours", author="me", subreddit="s"),
                         _t3("theirs", title="theirs", author="other", subreddit="s")])

    _reddit_transport(monkeypatch, handler)
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    items = asyncio.run(_rd().search_content("t", account, query="x"))
    assert [i["id"] for i in items] == ["t3_theirs"]


def test_reddit_search_with_nothing_to_search_returns_empty(monkeypatch):
    called = {"n": 0}
    _reddit_transport(monkeypatch, lambda r: (called.__setitem__("n", called["n"] + 1)
                                              or _listing([])))
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    assert asyncio.run(_rd().search_content("t", account, query="", subreddit="")) == []
    assert called["n"] == 0


# --- Reddit.reply_to_target ---------------------------------------------------------- #

def test_reddit_reply_adds_the_t3_prefix_and_returns_the_permalink(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"json": {"errors": [], "data": {"things": [
            {"data": {"name": "t1_new", "permalink": "/r/s/comments/x/_/new/"}}]}}})

    requests = _reddit_transport(monkeypatch, handler)
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    result = asyncio.run(_rd().reply_to_target(
        "t", account, target_type="submission", target_id="abc", text="hi"))

    from urllib.parse import parse_qs
    form = {k: v[0] for k, v in parse_qs(requests[0].content.decode()).items()}
    assert form["thing_id"] == "t3_abc"                     # prefix added from target_type
    assert form["text"] == "hi"
    assert result["id"] == "t1_new"
    assert result["url"] == "https://www.reddit.com/r/s/comments/x/_/new/"


def test_reddit_reply_keeps_an_existing_prefix(monkeypatch):
    requests = _reddit_transport(monkeypatch, lambda r: httpx.Response(
        200, json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_x"}}]}}}))
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    asyncio.run(_rd().reply_to_target("t", account, target_type="comment",
                                      target_id="t1_parent", text="hi"))
    from urllib.parse import parse_qs
    form = {k: v[0] for k, v in parse_qs(requests[0].content.decode()).items()}
    assert form["thing_id"] == "t1_parent"


def test_reddit_reply_raises_on_a_rejection(monkeypatch):
    _reddit_transport(monkeypatch, lambda r: httpx.Response(
        200, json={"json": {"errors": [["RATELIMIT", "you are doing that too much"]]}}))
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_rd().reply_to_target("t", account, target_type="submission",
                                          target_id="t3_x", text="hi"))
    assert "too much" in str(exc.value)


# --- the outreach tools -------------------------------------------------------------- #

def _invoke(tool, **kwargs):
    from agents import RunConfig
    from agents.tool_context import ToolContext

    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="1",
                      tool_arguments="{}", run_config=RunConfig())
    return asyncio.run(tool.on_invoke_tool(ctx, json.dumps(kwargs)))


def _state(store, account, *, targets=""):
    instruction = Instruction(name="i", task_type=InstructionTask.outreach,
                              engagement_targets=targets)
    return {"account": account, "store": store, "instruction": instruction,
            "run": None, "assets": []}


def _tool(state, name):
    return next((t for t in build_tools(state) if t.name == name), None)


def test_search_tools_gate_on_platform(store):
    x = store.upsert_account(Account(platform=PlatformName.twitter, external_id="9"),
                             access_token="t")
    rd = store.upsert_account(Account(platform=PlatformName.reddit, external_id="8"),
                              access_token="t")
    ig = store.upsert_account(Account(platform=PlatformName.instagram, external_id="7"),
                              access_token="t")
    assert _tool(_state(store, x), "x_search_posts") is not None
    assert _tool(_state(store, rd), "reddit_search_posts") is not None
    assert _tool(_state(store, rd), "reddit_reply") is not None
    # Wrong platform never sees the other's search tool.
    assert _tool(_state(store, ig), "x_search_posts") is None
    assert _tool(_state(store, ig), "reddit_search_posts") is None
    assert _tool(_state(store, x), "reddit_search_posts") is None


def test_x_search_tool_falls_back_to_instruction_targets(store, monkeypatch):
    from aismm.platforms import twitter as tw

    seen = {}

    async def fake_search(self, token, account, *, query, limit=10, subreddit=""):
        seen["query"] = query
        return [{"id": "1", "text": "hi", "author": "@a"}]

    monkeypatch.setattr(tw.Twitter, "search_content", fake_search)
    x = store.upsert_account(Account(platform=PlatformName.twitter, handle="me",
                                     external_id="9"), access_token="t")
    tool = _tool(_state(store, x, targets="prompt engineering, #AI"), "x_search_posts")
    result = _invoke(tool)                       # no explicit query → use targets
    assert seen["query"] == '"prompt engineering" OR #AI'
    assert result["count"] == 1


def test_x_search_tool_reports_when_there_is_nothing_to_search(store):
    x = store.upsert_account(Account(platform=PlatformName.twitter, handle="me",
                                     external_id="9"), access_token="t")
    tool = _tool(_state(store, x, targets=""), "x_search_posts")
    result = _invoke(tool)                       # no query, no targets
    assert result["error"] == "no_query"


def test_x_search_tool_annotates_already_answered(store, monkeypatch):
    from aismm import engagement_ledger
    from aismm.platforms import twitter as tw

    async def fake_search(self, token, account, *, query, limit=10, subreddit=""):
        return [{"id": "seen1", "text": "a"}, {"id": "fresh2", "text": "b"}]

    monkeypatch.setattr(tw.Twitter, "search_content", fake_search)
    x = store.upsert_account(Account(platform=PlatformName.twitter, handle="me",
                                     external_id="9"), access_token="t")
    engagement_ledger.record(x, store, "tweet", "seen1")
    tool = _tool(_state(store, x, targets="ai"), "x_search_posts")
    posts = {p["id"]: p["already_answered"] for p in _invoke(tool)["posts"]}
    assert posts == {"seen1": True, "fresh2": False}


def test_reddit_plan_searches_derives_from_targets(store):
    from aismm.tools import reddit_tools

    rd = store.upsert_account(Account(platform=PlatformName.reddit, external_id="8"),
                              access_token="t")
    state = _state(store, rd, targets="agents, r/MachineLearning, r/LocalLLaMA")
    plan = reddit_tools._plan_searches(state, "", "")
    # each target subreddit (with the keyword query), plus one site-wide keyword search
    assert ("agents", "MachineLearning") in plan
    assert ("agents", "LocalLLaMA") in plan
    assert ("agents", "") in plan


def test_reddit_plan_searches_honours_explicit_args(store):
    from aismm.tools import reddit_tools

    rd = store.upsert_account(Account(platform=PlatformName.reddit, external_id="8"),
                              access_token="t")
    state = _state(store, rd, targets="agents, r/MachineLearning")
    # An explicit call is one search, verbatim — the platform strips the r/ prefix.
    assert reddit_tools._plan_searches(state, "custom", "r/foo") == [("custom", "r/foo")]


def test_reddit_search_tool_reports_when_no_targets(store):
    rd = store.upsert_account(Account(platform=PlatformName.reddit, external_id="8"),
                              access_token="t")
    tool = _tool(_state(store, rd, targets=""), "reddit_search_posts")
    assert _invoke(tool)["error"] == "no_targets"


def test_reddit_reply_tool_routes_through_the_gate(store, monkeypatch):
    from aismm import engagement

    seen = {}

    async def fake_perform_reply(state, *, target_type, target_id, text, target_excerpt=""):
        seen.update(target_type=target_type, target_id=target_id,
                    text=text, excerpt=target_excerpt)
        return {"status": "staged"}

    monkeypatch.setattr(engagement, "perform_reply", fake_perform_reply)
    rd = store.upsert_account(Account(platform=PlatformName.reddit, external_id="8"),
                              access_token="t")
    tool = _tool(_state(store, rd), "reddit_reply")
    result = _invoke(tool, post_id="t3_x", text="great point", replying_to="the OP")
    assert result == {"status": "staged"}
    assert seen == {"target_type": "submission", "target_id": "t3_x",
                    "text": "great point", "excerpt": "the OP"}


# --- prompt + registry wiring -------------------------------------------------------- #

def _caps():
    return get_platform(PlatformName.twitter).capabilities


def test_outreach_kickoff_lists_configured_targets():
    from aismm.agent.prompts import build_outreach_kickoff

    instr = Instruction(name="i", brief="be helpful", task_type=InstructionTask.outreach,
                        engagement_targets="agents, #AI, r/MachineLearning")
    account = Account(platform=PlatformName.twitter, handle="me", external_id="9")
    text = build_outreach_kickoff(account=account, instruction=instr, platform_caps=_caps())
    assert "OUTREACH TARGETS (search these first" in text
    assert "#AI" in text and "r/MachineLearning" in text
    assert "finish_engagement" in text


def test_outreach_kickoff_asks_to_infer_when_no_targets():
    from aismm.agent.prompts import build_outreach_kickoff

    instr = Instruction(name="i", brief="be helpful", task_type=InstructionTask.outreach)
    account = Account(platform=PlatformName.twitter, handle="me", external_id="9")
    text = build_outreach_kickoff(account=account, instruction=instr, platform_caps=_caps())
    assert "none set — INFER" in text


def test_build_tools_gives_outreach_the_engage_terminal_not_publish(store):
    x = store.upsert_account(Account(platform=PlatformName.twitter, external_id="9"),
                             access_token="t")
    names = {t.name for t in build_tools(_state(store, x))}
    assert "finish_engagement" in names
    assert "report_failure" in names
    assert "publish" not in names               # an outreach run must never post


def test_always_on_for_outreach_matches_engage():
    from aismm.tools.registry import ALWAYS_ON_ENGAGE, always_on_for

    assert always_on_for("outreach") == ALWAYS_ON_ENGAGE


# --- the run-time capability guard --------------------------------------------------- #

def test_outreach_is_unsupported_on_instagram():
    """The reported failure: an outreach shift on Instagram has no search tools, so
    the guard skips it BEFORE the agent runs instead of hard-failing the run."""
    from aismm.orchestrator import task_unsupported_reason

    caps = get_platform(PlatformName.instagram).capabilities
    assert task_unsupported_reason(InstructionTask.outreach, caps)     # blocked
    # Engaging on Instagram (its own comments/DMs) IS supported.
    assert task_unsupported_reason(InstructionTask.engage, caps) == ""


def test_outreach_is_supported_on_x_and_reddit():
    from aismm.orchestrator import task_unsupported_reason

    for name in (PlatformName.twitter, PlatformName.reddit):
        caps = get_platform(name).capabilities
        assert task_unsupported_reason(InstructionTask.outreach, caps) == ""


def test_engage_is_unsupported_on_tiktok():
    """TikTok has no comment or DM API for third-party apps."""
    from aismm.orchestrator import task_unsupported_reason

    caps = get_platform(PlatformName.tiktok).capabilities
    assert task_unsupported_reason(InstructionTask.engage, caps)


@pytest.mark.parametrize("task", [InstructionTask.publish, InstructionTask.auto])
def test_publish_and_auto_are_never_blocked(task):
    """Every platform posts, and auto falls back to posting — never guarded out."""
    from aismm.orchestrator import task_unsupported_reason

    for name in PlatformName:
        caps = get_platform(name).capabilities
        assert task_unsupported_reason(task, caps) == ""


def test_run_one_skips_an_outreach_run_on_instagram(store, monkeypatch):
    """End to end: no Run row is created, the account is reported skipped, and the
    agent is never invoked."""
    from aismm import orchestrator

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id="9", handle="me"),
        access_token="t")
    instr = store.upsert_instruction(
        Instruction(name="ig outreach", task_type=InstructionTask.outreach))

    def _boom(*a, **k):
        raise AssertionError("run_for_account must not be called for a skipped task")

    monkeypatch.setattr(orchestrator, "run_for_account", _boom)
    result = orchestrator._run_one(instr, account, store)
    assert result["status"] == "skipped"
    assert result["reason"] == "task_unsupported"
    assert store.count_runs(instruction_id=instr.id) == 0
