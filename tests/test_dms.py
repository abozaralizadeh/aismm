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


def test_x_list_dms_reports_a_failure_instead_of_pretending_there_are_none(monkeypatch):
    """"Cannot read DMs" and "no DMs" are different answers.

    Swallowing the first into an empty list is what let a broken Instagram DM
    read look like a quiet inbox. The tool layer catches this and hands the agent
    the reason.
    """
    from aismm.platforms import twitter as tw

    async def boom(self, *a, **kw):
        raise RuntimeError("402 no credits")

    monkeypatch.setattr(tw.Twitter, "_get", boom)
    account = Account(platform=PlatformName.twitter, handle="me", external_id="9")
    with pytest.raises(RuntimeError, match="402"):
        _run(_x().list_dms("t", account))


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


def test_reddit_list_dms_reports_a_failure_instead_of_pretending_there_are_none(monkeypatch):
    _rd_transport(monkeypatch, lambda r: httpx.Response(500, json={}))
    account = Account(platform=PlatformName.reddit, handle="me", external_id="9")
    with pytest.raises(httpx.HTTPStatusError):
        _run(_rd().list_dms("t", account))


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
IG_PAGE = "998877665544332"


def _ig_account(page_id=IG_PAGE):
    """A connected Instagram account, with the linked Page recorded on it."""
    account = Account(platform=PlatformName.instagram, handle="brand",
                      external_id=IG_USER)
    if page_id:
        account.set_meta({"page_id": page_id})
    return account


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
    account = _ig_account()
    items = _run(_ig().list_dms("t", account))
    assert [i["id"] for i in items] == ["m1"]           # our own outbound is dropped
    assert items[0]["conversation_id"] == "555"         # reply is addressed to the sender IGSID
    assert items[0]["sender"] == "fan"


def test_ig_reply_to_dm_posts_to_the_recipient(monkeypatch):
    def handler(request):
        assert request.url.path.endswith(f"/{IG_PAGE}/messages")
        from urllib.parse import parse_qs
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert json.loads(form["recipient"]) == {"id": "555"}
        assert json.loads(form["message"]) == {"text": "thank you!"}
        return httpx.Response(200, json={"message_id": "mid_1", "recipient_id": "555"})

    requests = _ig_transport(monkeypatch, handler)
    account = _ig_account()
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


# --- Instagram messaging hangs off the PAGE, not the IG user --------------------------- #
# Reported: "the engagement in instagram never sees the DMs and never answers
# them". Two causes, and the second is why the first was invisible for so long:
#
#   1. /conversations and /messages were addressed to the IG user id. With
#      Instagram-via-Facebook-Login every messaging endpoint is on the linked
#      PAGE (Meta's own guide: GET /{page-id}/conversations?platform=instagram).
#   2. The resulting error was swallowed into an empty list, so an account that
#      COULD NOT read DMs looked exactly like an account with none.

def test_the_conversations_read_goes_to_the_page(monkeypatch):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["platform"] = request.url.params.get("platform")
        return httpx.Response(200, json={"data": []})

    _ig_transport(monkeypatch, handler)
    _run(_ig().list_dms("t", _ig_account()))
    assert seen["path"].endswith(f"/{IG_PAGE}/conversations")
    assert IG_USER not in seen["path"]          # the old, wrong node
    assert seen["platform"] == "instagram"


def test_an_account_connected_before_the_page_id_was_stored_still_works(monkeypatch):
    """`me` resolves to the token's owner, and the stored token IS the page token
    (fetch_identity refuses the connection otherwise) — so no reconnect is needed."""
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": []})

    _ig_transport(monkeypatch, handler)
    _run(_ig().list_dms("t", _ig_account(page_id=None)))
    assert seen["path"].endswith("/me/conversations")


def test_a_refused_conversations_read_is_reported_not_silently_empty(monkeypatch):
    _ig_transport(monkeypatch, lambda r: httpx.Response(
        400, json={"error": {"message": "(#200) Requires pages_manage_metadata",
                             "code": 200}}))
    with pytest.raises(Exception, match="pages_manage_metadata"):
        _run(_ig().list_dms("t", _ig_account()))


def test_the_agent_is_told_why_rather_than_seeing_an_empty_inbox(monkeypatch):
    """End to end: the tool layer turns the refusal into something actionable."""
    import asyncio as _asyncio

    from aismm.tools import instagram_tools

    _ig_transport(monkeypatch, lambda r: httpx.Response(
        400, json={"error": {"message": "(#200) Requires pages_manage_metadata"}}))

    async def context(_state):
        return _ig(), _ig_account(), "token"

    monkeypatch.setattr(instagram_tools, "_instagram_context", context)
    result = _asyncio.run(instagram_tools._with_context(
        {}, lambda p, a, t: p.list_dms(t, a)))
    assert result["error"] == "instagram_api_error"
    assert "pages_manage_metadata" in result["message"]


def test_our_own_outbound_message_is_never_answered(monkeypatch):
    """A thread carries both sides; the account must not reply to itself. The
    Page and the IG user both count as us."""
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "c1", "messages": {"data": [
            {"id": "m1", "from": {"id": "555", "username": "fan"},
             "message": "hello?", "created_time": "2026-08-02T00:00:00Z"},
            {"id": "m2", "from": {"id": IG_USER, "username": "brand"},
             "message": "ours, via the ig id", "created_time": "2026-08-01T00:00:00Z"},
            {"id": "m3", "from": {"id": IG_PAGE, "username": "brand"},
             "message": "ours, via the page", "created_time": "2026-08-01T00:00:00Z"},
        ]}}]})

    _ig_transport(monkeypatch, handler)
    items = _run(_ig().list_dms("t", _ig_account()))
    assert [i["id"] for i in items] == ["m1"]


def test_the_page_id_is_recorded_at_connect(monkeypatch):
    """Without it every existing connection would need a reconnect."""
    def handler(request):
        if request.url.path.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": [{
                "id": IG_PAGE, "name": "Brand Page", "access_token": "page-token",
                "instagram_business_account": {"id": IG_USER, "username": "brand"}}]})
        return httpx.Response(200, json={})

    _ig_transport(monkeypatch, handler)
    identity = _run(_ig().fetch_identity("user-token"))
    assert identity.meta["page_id"] == IG_PAGE
    assert identity.external_id == IG_USER          # publishing still uses the IG id

    identities = _run(_ig().fetch_identities("user-token"))
    assert identities[0].meta["page_id"] == IG_PAGE


def test_the_dm_scope_is_requested():
    assert "instagram_manage_messages" in _ig().DEFAULT_SCOPES


def test_the_dm_scope_stays_optional():
    """One unavailable scope kills the WHOLE dialog, so it must never be able to
    take publishing down with it."""
    ig = _ig()
    assert "instagram_manage_messages" in ig.OPTIONAL_SCOPES
    assert "instagram_manage_messages" not in ig.REQUIRED_SCOPES


def test_pages_manage_metadata_is_never_requested_by_default():
    """Adding it to the default set broke a working login outright:

        Invalid Scopes: pages_manage_metadata

    It is not offered on every app's Permissions and Features page, so an app
    cannot even ask for it — and Meta refuses the WHOLE dialog over one scope it
    does not have, taking publishing down with it. Meta's older Messenger guide
    lists it for Instagram messaging, mostly for webhook subscription, which this
    app does not use: it polls /conversations instead. Opt in via INSTAGRAM_SCOPES
    if your app has it and the read is genuinely refused without it.
    """
    ig = _ig()
    assert "pages_manage_metadata" not in ig.DEFAULT_SCOPES
    assert "pages_manage_metadata" not in ig.scopes
    assert "pages_manage_metadata" in ig.EXTRA_SCOPES     # documented, not requested


def test_a_refused_read_names_what_to_check(monkeypatch):
    """The likely causes are all invisible from the error alone: a scope never
    granted, a Page the login missed, or an account connected before any of it."""
    _ig_transport(monkeypatch, lambda r: httpx.Response(
        400, json={"error": {"message": "(#200) Permissions error", "code": 200}}))
    with pytest.raises(RuntimeError) as caught:
        _run(_ig().list_dms("t", _ig_account()))
    message = str(caught.value)
    assert "Permissions error" in message           # Graph's own words survive
    assert "RECONNECTED" in message
    assert "instagram_manage_messages" in message
    assert "linked Page" in message


def test_the_failure_names_the_node_it_asked(monkeypatch):
    """Page id vs `me` vs the IG user id is the first thing to check."""
    _ig_transport(monkeypatch, lambda r: httpx.Response(400, json={"error": {}}))
    with pytest.raises(RuntimeError, match=IG_PAGE):
        _run(_ig().list_dms("t", _ig_account()))
