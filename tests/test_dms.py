"""Direct messages: read the inbox and answer, per platform (X, IG, Reddit).

DMs ride the SAME engagement gate as comments (``perform_reply`` — covered in
``test_engagement.py``); what is platform-specific, and pinned here, is the two-id
shape. ``list_dms`` normalises each inbound message to ``id`` (the message — the
ledger dedupe key) and ``conversation_id`` (where a reply is sent), and
``reply_to_target``'s DM branch sends to that conversation, NOT to ``target_id``.
YouTube and TikTok have no DM API, so they inherit the refusing base method.

Network is mocked throughout; nothing here sends a message or spends a credit.
"""
import asyncio
import json

import httpx
import pytest

from aismm.models import Account, Instruction, InstructionTask, PlatformName
from aismm.platforms.registry import get_platform
from aismm.tools.registry import build_tools


def _run(coro):
    return asyncio.run(coro)


# --- capabilities + the base contract ------------------------------------------------ #

def test_only_x_ig_reddit_declare_dms():
    dm_capable = {n.value for n in PlatformName
                  if getattr(get_platform(n).capabilities, "supports_dms", False)}
    assert dm_capable == {"twitter", "instagram", "reddit"}


@pytest.mark.parametrize("name", [PlatformName.youtube, PlatformName.tiktok])
def test_a_platform_without_dms_raises_the_base_method(name):
    """YouTube/TikTok have no DM API — a stray list_dms fails loudly instead of
    pretending to return an inbox."""
    platform = get_platform(name)
    assert platform.capabilities.supports_dms is False
    account = Account(platform=name, external_id="1")
    with pytest.raises(RuntimeError):
        _run(platform.list_dms("t", account))


# --- X: list_dms / send_dm / reply_to_target ----------------------------------------- #

def _x():
    return get_platform(PlatformName.twitter)


def test_x_list_dms_keeps_inbound_messages_and_drops_our_own(monkeypatch):
    from aismm.platforms import twitter as tw

    seen = {}

    async def fake_get(self, access_token, path, params):
        seen["path"] = path
        seen["params"] = params
        return {
            "data": [
                {"id": "e1", "text": "hi there", "event_type": "MessageCreate",
                 "sender_id": "111", "dm_conversation_id": "conv-a",
                 "created_at": "2026-08-01T00:00:00Z"},
                {"id": "e2", "text": "my own reply", "event_type": "MessageCreate",
                 "sender_id": "9", "dm_conversation_id": "conv-a"},   # us — dropped
                {"id": "e3", "text": "", "event_type": "ParticipantsJoin",
                 "sender_id": "111", "dm_conversation_id": "conv-a"},  # not a message
            ],
            "includes": {"users": [{"id": "111", "username": "someone"}]},
        }

    monkeypatch.setattr(tw.Twitter, "_get", fake_get)
    account = Account(platform=PlatformName.twitter, handle="me", external_id="9")
    items = _run(_x().list_dms("t", account, limit=25))

    assert seen["path"] == "dm_events"
    assert [i["id"] for i in items] == ["e1"]           # only the stranger's message
    assert items[0]["conversation_id"] == "conv-a"      # where a reply is sent
    assert items[0]["sender"] == "@someone"
    assert items[0]["text"] == "hi there"


def test_x_list_dms_is_best_effort_on_failure(monkeypatch):
    from aismm.platforms import twitter as tw

    async def boom(self, *a, **kw):
        raise RuntimeError("402 no credits")

    monkeypatch.setattr(tw.Twitter, "_get", boom)
    account = Account(platform=PlatformName.twitter, handle="me", external_id="9")
    assert _run(_x().list_dms("t", account)) == []


def _x_transport(monkeypatch, handler):
    from aismm.platforms import twitter as tw

    requests: list[httpx.Request] = []

    def record(request):
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    real = httpx.AsyncClient
    monkeypatch.setattr(tw.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    return requests


def test_x_reply_to_dm_sends_into_the_conversation(monkeypatch):
    def handler(request):
        assert request.url.path == "/2/dm_conversations/conv-a/messages"
        assert json.loads(request.content.decode()) == {"text": "on it!"}
        return httpx.Response(201, json={"data": {"dm_event_id": "ev123"}})

    requests = _x_transport(monkeypatch, handler)
    account = Account(platform=PlatformName.twitter, handle="me", external_id="9")
    result = _run(_x().reply_to_target(
        "t", account, target_type="dm", target_id="e1", text="on it!",
        reply_to="conv-a"))
    assert result["id"] == "ev123"
    assert len(requests) == 1


def test_x_reply_to_dm_without_a_conversation_refuses(monkeypatch):
    account = Account(platform=PlatformName.twitter, handle="me", external_id="9")
    with pytest.raises(RuntimeError):
        _run(_x().reply_to_target("t", account, target_type="dm", target_id="e1",
                                  text="hi", reply_to=""))


# --- Reddit: list_dms / reply_to_target ---------------------------------------------- #

def _rd():
    return get_platform(PlatformName.reddit)


def _rd_transport(monkeypatch, handler):
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


def _t4(id_, **data):
    return {"kind": "t4", "data": {"name": f"t4_{id_}", "id": id_, **data}}


def test_reddit_list_dms_keeps_pms_and_drops_comment_replies_and_self(monkeypatch):
    def handler(request):
        assert "/message/inbox" in str(request.url)
        return httpx.Response(200, json={"data": {"children": [
            _t4("aaa", subject="hello", body="a question", author="someone",
                created_utc=1_700_000_000.0),
            _t4("bbb", body="a comment reply", author="x", was_comment=True),  # dropped
            _t4("ccc", body="my own message", author="me"),                    # us — dropped
            {"kind": "t1", "data": {"name": "t1_z", "body": "not a pm"}},       # not t4
        ]}})

    _rd_transport(monkeypatch, handler)
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    items = _run(_rd().list_dms("t", account))
    assert [i["id"] for i in items] == ["t4_aaa"]       # fullname is the id
    assert items[0]["conversation_id"] == ""            # Reddit addresses by fullname
    assert items[0]["sender"] == "u/someone"
    assert items[0]["text"].startswith("hello")


def test_reddit_list_dms_is_best_effort_on_failure(monkeypatch):
    _rd_transport(monkeypatch, lambda r: httpx.Response(500, json={}))
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    assert _run(_rd().list_dms("t", account)) == []


def test_reddit_reply_to_dm_addresses_the_t4_fullname(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"json": {"errors": [], "data": {"things": [
            {"data": {"name": "t4_new"}}]}}})

    requests = _rd_transport(monkeypatch, handler)
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    # reply_to is empty for a Reddit DM — the message fullname IS the destination.
    _run(_rd().reply_to_target("t", account, target_type="dm", target_id="t4_aaa",
                               text="thanks!", reply_to=""))
    from urllib.parse import parse_qs
    form = {k: v[0] for k, v in parse_qs(requests[0].content.decode()).items()}
    assert form["thing_id"] == "t4_aaa"                 # kept its t4_ prefix
    assert form["text"] == "thanks!"


def test_reddit_reply_to_dm_adds_the_t4_prefix_when_missing(monkeypatch):
    requests = _rd_transport(monkeypatch, lambda r: httpx.Response(
        200, json={"json": {"errors": [], "data": {"things": [{"data": {"name": "t4_x"}}]}}}))
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    _run(_rd().reply_to_target("t", account, target_type="dm", target_id="bare",
                               text="hi", reply_to=""))
    from urllib.parse import parse_qs
    form = {k: v[0] for k, v in parse_qs(requests[0].content.decode()).items()}
    assert form["thing_id"] == "t4_bare"


# --- Instagram: list_dms / send_dm / reply_to_target --------------------------------- #

IG_USER = "17841400000000000"


def _ig():
    return get_platform(PlatformName.instagram)


def _ig_transport(monkeypatch, handler):
    from aismm.platforms import instagram as ig

    requests: list[httpx.Request] = []

    def record(request):
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    real = httpx.AsyncClient
    monkeypatch.setattr(ig.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": transport}))
    return requests


def test_ig_list_dms_keeps_inbound_and_uses_sender_as_the_recipient(monkeypatch):
    def handler(request):
        assert "/conversations" in str(request.url)
        return httpx.Response(200, json={"data": [
            {"id": "conv-1", "messages": {"data": [
                {"id": "m1", "from": {"id": "555", "username": "fan"},
                 "message": "love your work", "created_time": "2026-08-02T00:00:00Z"},
                {"id": "m2", "from": {"id": IG_USER, "username": "brand"},
                 "message": "our own reply", "created_time": "2026-08-01T00:00:00Z"},
            ]}},
        ]})

    _ig_transport(monkeypatch, handler)
    account = Account(platform=PlatformName.instagram, handle="brand", external_id=IG_USER)
    items = _run(_ig().list_dms("t", account))
    assert [i["id"] for i in items] == ["m1"]           # our own outbound is dropped
    assert items[0]["conversation_id"] == "555"         # reply is addressed to the sender IGSID
    assert items[0]["sender"] == "fan"


def test_ig_reply_to_dm_posts_to_the_recipient(monkeypatch):
    def handler(request):
        assert request.url.path.endswith(f"/{IG_USER}/messages")
        from urllib.parse import parse_qs
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert json.loads(form["recipient"]) == {"id": "555"}
        assert json.loads(form["message"]) == {"text": "thank you!"}
        return httpx.Response(200, json={"message_id": "mid_1", "recipient_id": "555"})

    requests = _ig_transport(monkeypatch, handler)
    account = Account(platform=PlatformName.instagram, handle="brand", external_id=IG_USER)
    result = _run(_ig().reply_to_target(
        "t", account, target_type="dm", target_id="m1", text="thank you!",
        reply_to="555"))
    assert result["id"] == "mid_1"
    assert len(requests) == 1


def test_ig_reply_to_dm_without_a_recipient_refuses(monkeypatch):
    account = Account(platform=PlatformName.instagram, handle="brand", external_id=IG_USER)
    with pytest.raises(RuntimeError):
        _run(_ig().reply_to_target("t", account, target_type="dm", target_id="m1",
                                   text="hi", reply_to=""))


# --- the DM tools -------------------------------------------------------------------- #

def _invoke(tool, **kwargs):
    from agents import RunConfig
    from agents.tool_context import ToolContext

    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="1",
                      tool_arguments="{}", run_config=RunConfig())
    return _run(tool.on_invoke_tool(ctx, json.dumps(kwargs)))


def _state(store, account):
    return {"account": account, "store": store,
            "instruction": Instruction(name="i", task_type=InstructionTask.engage),
            "run": None, "assets": []}


def _tool(state, name):
    return next((t for t in build_tools(state) if t.name == name), None)


def test_dm_tools_gate_on_platform(store):
    x = store.upsert_account(Account(platform=PlatformName.twitter, external_id="9"),
                             access_token="t")
    rd = store.upsert_account(Account(platform=PlatformName.reddit, external_id="8"),
                              access_token="t")
    ig = store.upsert_account(Account(platform=PlatformName.instagram, external_id="7"),
                              access_token="t")
    yt = store.upsert_account(Account(platform=PlatformName.youtube, external_id="6"),
                              access_token="t")
    assert _tool(_state(store, x), "x_dms") is not None
    assert _tool(_state(store, x), "x_reply_to_dm") is not None
    assert _tool(_state(store, rd), "reddit_dms") is not None
    assert _tool(_state(store, ig), "instagram_dms") is not None
    assert _tool(_state(store, ig), "instagram_reply_to_dm") is not None
    # YouTube has no DM API → no DM tools; and no cross-platform leakage.
    assert _tool(_state(store, yt), "x_dms") is None
    assert _tool(_state(store, x), "instagram_dms") is None


def test_every_dm_tool_is_registered():
    from aismm.tools.registry import registered_tool_names

    names = registered_tool_names()
    for expected in ("x_dms", "x_reply_to_dm", "reddit_dms", "reddit_reply_to_dm",
                     "instagram_dms", "instagram_reply_to_dm"):
        assert expected in names


def test_x_dm_read_tool_annotates_already_answered(store, monkeypatch):
    from aismm import engagement_ledger
    from aismm.platforms import twitter as tw

    async def fake_list(self, token, account, *, limit=25):
        return [{"id": "seen", "conversation_id": "c1", "text": "a"},
                {"id": "fresh", "conversation_id": "c2", "text": "b"}]

    monkeypatch.setattr(tw.Twitter, "list_dms", fake_list)
    x = store.upsert_account(Account(platform=PlatformName.twitter, handle="me",
                                     external_id="9"), access_token="t")
    engagement_ledger.record(x, store, "dm", "seen")
    tool = _tool(_state(store, x), "x_dms")
    flags = {d["id"]: d["already_answered"] for d in _invoke(tool)["dms"]}
    assert flags == {"seen": True, "fresh": False}


def test_x_reply_to_dm_tool_passes_the_conversation_as_reply_to(store, monkeypatch):
    from aismm import engagement

    seen = {}

    async def fake_perform_reply(state, *, target_type, target_id, text,
                                 target_excerpt="", reply_to=""):
        seen.update(target_type=target_type, target_id=target_id, text=text,
                    reply_to=reply_to, excerpt=target_excerpt)
        return {"status": "staged"}

    monkeypatch.setattr(engagement, "perform_reply", fake_perform_reply)
    x = store.upsert_account(Account(platform=PlatformName.twitter, external_id="9"),
                             access_token="t")
    tool = _tool(_state(store, x), "x_reply_to_dm")
    result = _invoke(tool, message_id="e1", conversation_id="conv-a", text="hi",
                     replying_to="their message")
    assert result == {"status": "staged"}
    assert seen == {"target_type": "dm", "target_id": "e1", "text": "hi",
                    "reply_to": "conv-a", "excerpt": "their message"}


def test_reddit_reply_to_dm_tool_sends_no_conversation(store, monkeypatch):
    from aismm import engagement

    seen = {}

    async def fake_perform_reply(state, *, target_type, target_id, text,
                                 target_excerpt="", reply_to=""):
        seen.update(target_type=target_type, target_id=target_id, reply_to=reply_to)
        return {"status": "staged"}

    monkeypatch.setattr(engagement, "perform_reply", fake_perform_reply)
    rd = store.upsert_account(Account(platform=PlatformName.reddit, external_id="8"),
                              access_token="t")
    tool = _tool(_state(store, rd), "reddit_reply_to_dm")
    _invoke(tool, message_id="t4_aaa", text="thanks", replying_to="a PM")
    # Reddit derives the destination from the fullname, so reply_to stays empty.
    assert seen == {"target_type": "dm", "target_id": "t4_aaa", "reply_to": ""}


def test_ig_reply_to_dm_tool_passes_the_recipient_as_reply_to(store, monkeypatch):
    from aismm import engagement

    seen = {}

    async def fake_perform_reply(state, *, target_type, target_id, text,
                                 target_excerpt="", reply_to=""):
        seen.update(target_type=target_type, target_id=target_id, reply_to=reply_to)
        return {"status": "staged"}

    monkeypatch.setattr(engagement, "perform_reply", fake_perform_reply)
    ig = store.upsert_account(Account(platform=PlatformName.instagram, external_id=IG_USER),
                              access_token="t")
    tool = _tool(_state(store, ig), "instagram_reply_to_dm")
    _invoke(tool, message_id="m1", recipient_id="555", message="hi there")
    assert seen == {"target_type": "dm", "target_id": "m1", "reply_to": "555"}
