"""X does not index replies made inside a Community, and a run must SAY so.

Reported as "the comments responses in X are not working — even if there are
comments, it does not respond at all", with a screenshot of an unanswered reply
under a Build-in-Public community post. The engage run had reported "no new
replyable comments or DMs were surfaced", with no error anywhere in the log.

Probed live against that exact post, which X itself reports as having one reply:

* ``GET /2/tweets/{id}``                                 -> ``reply_count: 1``
* ``search/recent?query=conversation_id:{id} is:reply``  -> the root post only
* ``search/recent?query=to:{handle}`` / ``from:{replier}`` -> not there
* ``GET /2/users/{id}/mentions``                         -> not there
* ``GET /2/communities/{id}/tweets``                     -> 404, no such endpoint
* ``community_id:`` as a search operator                 -> "invalid operator"

X has no "get replies" endpoint, so every reply-reading path is a recent search —
and the reply is not in the index. It cannot be read. The account posts on a
community rotation (``next_community``), so EVERY post it makes is one of these,
and the run truthfully found nothing while never being able to look.

That is the same class of bug as an unreadable Instagram inbox: what a run could
not check is recorded in code and reported, because "no comments" and "I could
not see the comments" are different answers.
"""
from __future__ import annotations

import asyncio

import pytest
from agents import RunConfig
from agents.tool_context import ToolContext

from aismm import engagement
from aismm.models import (Account, Instruction, InstructionTask, PlatformName, Run,
                          RunStatus)
from aismm.platforms import twitter as tw
from aismm.platforms.registry import get_platform
from aismm.tools import twitter_tools
from aismm.tools.engagement_finish import perform_finish_engagement


def _account():
    return Account(platform=PlatformName.twitter, handle="me", external_id="9")


def _x():
    return get_platform(PlatformName.twitter)


def _wire(monkeypatch, posts, replies=()):
    """Fake X: ``/users/:id/tweets`` returns ``posts``, recent search ``replies``."""
    calls: list[dict] = []

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, data):
            self._data = data

        def json(self):
            return {"data": self._data}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            calls.append({"url": url, "params": kw.get("params", {})})
            if "search/recent" in url:
                return _Resp(list(replies))
            return _Resp(list(posts))

    monkeypatch.setattr(tw.httpx, "AsyncClient", lambda **kw: _Client())
    return calls


# --- the platform stops asking a question X cannot answer -------------------------------- #

def test_a_community_post_is_left_out_of_the_reply_search(monkeypatch):
    calls = _wire(monkeypatch, [{"id": "P1"},
                                {"id": "P2", "community_id": "149344"},
                                {"id": "P3"}])
    asyncio.run(_x().list_replies("t", _account()))
    query = [c for c in calls if "search/recent" in c["url"]][0]["params"]["query"]
    assert "conversation_id:P1" in query and "conversation_id:P3" in query
    assert "P2" not in query


def test_an_account_that_only_posts_to_communities_makes_no_search_at_all(monkeypatch):
    """A guaranteed-empty search still costs a request AND every object it returns.

    X bills a read twice over, so the saving is real — but the point is that the
    empty answer was indistinguishable from "nobody replied".
    """
    calls = _wire(monkeypatch, [{"id": "P1", "community_id": "149344"},
                                {"id": "P2", "community_id": "169980"}])
    assert asyncio.run(_x().list_replies("t", _account())) == []
    assert not [c for c in calls if "search/recent" in c["url"]]


def test_community_id_is_asked_for_on_every_tweet_read():
    """Detection is only possible because the field rides along for free."""
    assert "community_id" in tw.Twitter.TWEET_FIELDS


def test_splitting_ignores_posts_with_no_id():
    readable, hidden = tw.split_reply_sources(
        [{"id": "P1"}, {"text": "no id"}, {"id": "P2", "community_id": "1"}])
    assert readable == ["P1"] and [p["id"] for p in hidden] == ["P2"]


# --- the tool tells the agent what it could not see --------------------------------------- #

def _tool_state(store):
    account = _account()
    store.upsert_account(account, access_token="t")
    instruction = Instruction(name="Abozar X Comments", brief="b",
                              task_type=InstructionTask.engage)
    store.upsert_instruction(instruction)
    run = Run(instruction_id=instruction.id, account_id=account.id,
              status=RunStatus.running)
    store.add_run(run)
    return {"account": account, "instruction": instruction, "store": store, "run": run,
            "tool_names": set()}


def _invoke(tool, arguments: str = "{}"):
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="1",
                      tool_arguments=arguments, run_config=RunConfig())
    return asyncio.run(tool.on_invoke_tool(ctx, arguments))


def _replies_tool(monkeypatch, store, posts, replies=()):
    state = _tool_state(store)
    platform = _x()

    async def context(_state):
        return platform, state["account"], "token"

    monkeypatch.setattr(twitter_tools, "_context", context)
    _wire(monkeypatch, posts, replies)
    return state, twitter_tools._make_replies(state)


def test_the_reply_tool_names_the_posts_it_could_not_check(monkeypatch, store):
    state, tool = _replies_tool(monkeypatch, store,
                                [{"id": "P1", "community_id": "149344"}])
    view = _invoke(tool, '{"limit": 10}')
    assert view["count"] == 0
    assert view["unreadable"]["community_posts"] == ["P1"]
    assert "Community" in view["unreadable"]["reason"]
    assert "NOT report that there were no comments" in view["unreadable"]["say_so"]


def test_an_ordinary_timeline_is_reported_as_fully_checked(monkeypatch, store):
    """No warning where there is nothing to warn about — the usual case."""
    state, tool = _replies_tool(monkeypatch, store, [{"id": "P1"}],
                               [{"id": "r1", "text": "nice", "author_id": "111"}])
    view = _invoke(tool, '{"limit": 10}')
    assert view["count"] == 1 and "unreadable" not in view
    assert state.get("unreadable_surfaces") in (None, [])


def test_the_blind_spot_is_recorded_in_code_not_only_told_to_the_model(monkeypatch, store):
    state, tool = _replies_tool(monkeypatch, store,
                                [{"id": "P1", "community_id": "1"},
                                 {"id": "P2", "community_id": "2"}])
    _invoke(tool, '{"limit": 10}')
    assert state["unreadable_surfaces"] == ["replies on 2 X community post(s)"]


# --- and the run ENDS saying it, instead of "nothing to do" ------------------------------- #

def _finish_state(store, **extra):
    account = _account()
    store.upsert_account(account, access_token="t")
    instruction = Instruction(name="Abozar X Comments", brief="b",
                              task_type=InstructionTask.engage)
    store.upsert_instruction(instruction)
    run = Run(instruction_id=instruction.id, account_id=account.id,
              status=RunStatus.running)
    store.add_run(run)
    return {"account": account, "instruction": instruction, "store": store, "run": run,
            "tool_names": set(), "read_tools_used": set(), **extra}


def test_a_run_that_could_not_read_community_replies_says_so(store):
    state = _finish_state(store, unreadable_surfaces=["replies on 3 X community post(s)"])
    asyncio.run(perform_finish_engagement(state, "Checked mentions."))
    log = store.get_run(state["run"].id).log
    assert "COULD NOT CHECK" in log and "3 X community post(s)" in log


def test_a_run_with_nothing_hidden_from_it_is_not_annotated(store):
    state = _finish_state(store)
    asyncio.run(perform_finish_engagement(state, "Nothing new."))
    assert "COULD NOT CHECK" not in store.get_run(state["run"].id).log


def test_the_same_surface_is_not_listed_twice(store):
    """Two reads of the same tool in one run is one blind spot, not two."""
    state: dict = {}
    engagement.note_unreadable(state, "replies on 2 X community post(s)")
    engagement.note_unreadable(state, "replies on 2 X community post(s)")
    engagement.note_unreadable(state, "")
    assert state["unreadable_surfaces"] == ["replies on 2 X community post(s)"]


@pytest.mark.parametrize("status", [RunStatus.skipped])
def test_being_unable_to_look_does_not_change_the_run_status(store, status):
    """It is a note on the outcome, not a failure — nothing was attempted."""
    state = _finish_state(store, unreadable_surfaces=["replies on 1 X community post(s)"])
    asyncio.run(perform_finish_engagement(state, ""))
    assert store.get_run(state["run"].id).status is status
