"""X: the publish contract, multi-image posts, and the engagement tools.

The first ever X publish died with ``Twitter.publish() got an unexpected keyword
argument 'asset_paths'`` — after the agent had browsed two sites, generated an
image and written its memory. `perform_publish` always passes ``asset_paths`` and
``placement``; Instagram grew them when carousels arrived and the other three
platforms never did. Python does not check override signatures, so nothing caught
it until a real post was attempted.
"""
import asyncio

import httpx
import pytest

from aismm.models import Account, Instruction, PlatformName, PublishMode, Run
from aismm.platforms import registry
from aismm.tools.registry import build_tools


def _x():
    return registry.get_platform(PlatformName.twitter)


# --- the contract that broke --------------------------------------------------------- #

def test_publish_accepts_the_arguments_the_tool_sends():
    import inspect

    inspect.signature(_x().publish).bind_partial(
        access_token="t", account=None, caption="c", asset_path="/a.jpg",
        media_kind="image", instruction=None, asset_paths=["/a.jpg"], placement="feed")


def test_x_declares_its_four_image_limit():
    caps = _x().capabilities
    assert caps.supports_carousel is True
    assert caps.max_carousel_items == 4


# --- posting ------------------------------------------------------------------------- #

@pytest.fixture()
def api(monkeypatch):
    """Record every X API call; return plausible responses."""
    from aismm.platforms import twitter as tw

    calls = {"uploads": [], "tweets": [], "gets": [], "deletes": [],
             "appends": [], "finalizes": []}

    class _Resp:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status
            self.text = str(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            if url.endswith("/media/upload/initialize"):
                calls["uploads"].append(kw.get("json", {}))
                return _Resp({"data": {"id": f"media{len(calls['uploads'])}"}})
            if url.endswith("/append"):
                calls["appends"].append((url, kw.get("data", {})))
                return _Resp({"data": {"expires_at": 1}})
            if url.endswith("/finalize"):
                calls["finalizes"].append(url)
                return _Resp({"data": {"id": url.split("/")[-2]}})
            calls["tweets"].append(kw.get("json", {}))
            return _Resp({"data": {"id": "1799"}})

        async def get(self, url, **kw):
            calls["gets"].append((url, kw.get("params", {})))
            if "/users/me" in url:
                return _Resp({"data": {"username": "abo0zar", "name": "Abozar",
                                       "description": "bio",
                                       "public_metrics": {"followers_count": 42,
                                                          "following_count": 7,
                                                          "tweet_count": 100}}})
            if "/mentions" in url or "/tweets" in url and "users/" in url:
                return _Resp({"data": [{"id": "1", "text": "hello",
                                        "public_metrics": {"like_count": 3}}]})
            return _Resp({"data": {"id": "1", "text": "one",
                                   "public_metrics": {"like_count": 5,
                                                      "impression_count": 900}}})

        async def delete(self, url, **kw):
            calls["deletes"].append(url)
            return _Resp({"data": {"deleted": True}})

    monkeypatch.setattr(tw.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(tw, "read_bytes", lambda p: b"\xff\xd8\xffdata")
    return calls


def _publish(**kwargs):
    account = Account(platform=PlatformName.twitter, handle="abo0zar", external_id="9")
    return asyncio.run(_x().publish(access_token="t", account=account, caption="hi",
                                    media_kind="image", **kwargs))


def test_a_single_image_post_still_works(api):
    result = _publish(asset_path="/a.jpg", asset_paths=None)
    assert result.external_id == "1799"
    assert result.url == "https://x.com/abo0zar/status/1799"
    assert len(api["uploads"]) == 1
    assert api["tweets"][0]["media"]["media_ids"] == ["media1"]


def test_transient_media_initialize_failure_is_retried(monkeypatch):
    from aismm.platforms import twitter as tw

    calls = {"initialize": 0}

    class _Resp:
        def __init__(self, status, payload):
            self.status_code, self._payload, self.text, self.headers = status, payload, "", {}

        def json(self):
            return self._payload

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, **kwargs):
            if url.endswith("/initialize"):
                calls["initialize"] += 1
                return (_Resp(503, {"title": "Service Unavailable"}) if calls["initialize"] == 1
                        else _Resp(200, {"data": {"id": "media1"}}))
            if url.endswith("/finalize"):
                return _Resp(200, {"data": {"id": "media1"}})
            return _Resp(200, {"data": {"id": "post1"}})

    monkeypatch.setattr(tw.httpx, "AsyncClient", lambda **kwargs: _Client())
    monkeypatch.setattr(tw, "read_bytes", lambda path: b"\xff\xd8\xffdata")
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(tw.asyncio, "sleep", no_sleep)
    account = Account(platform=PlatformName.twitter, external_id="9")
    asyncio.run(_x().publish(access_token="t", account=account, caption="hi", asset_path="/a.jpg",
                             asset_paths=None, media_kind="image"))
    assert calls["initialize"] == 2


def test_a_community_target_is_sent_with_the_post(api):
    account = Account(platform=PlatformName.twitter, handle="a", external_id="9")
    account.set_meta({"community_id": "123"})
    asyncio.run(_x().publish(access_token="t", account=account, caption="hi", asset_path="",
                             asset_paths=None, media_kind="text"))
    assert api["tweets"][0]["community_id"] == "123"


def test_four_images_are_all_attached(api):
    _publish(asset_path="/a.jpg", asset_paths=[f"/{n}.jpg" for n in range(4)])
    assert len(api["uploads"]) == 4
    assert len(api["tweets"][0]["media"]["media_ids"]) == 4


def test_a_video_takes_only_the_first_asset(api):
    """X allows one video per post, never a video plus images."""
    account = Account(platform=PlatformName.twitter, handle="a", external_id="9")
    asyncio.run(_x().publish(access_token="t", account=account, caption="hi",
                             asset_path="/a.mp4", media_kind="video",
                             asset_paths=["/a.mp4", "/b.mp4"]))
    assert len(api["uploads"]) == 1


def test_a_text_only_post_uploads_nothing(api):
    account = Account(platform=PlatformName.twitter, handle="a", external_id="9")
    asyncio.run(_x().publish(access_token="t", account=account, caption="just text",
                             asset_path="", media_kind="text", asset_paths=None))
    assert api["uploads"] == []
    assert "media" not in api["tweets"][0]


# --- the media upload contract ------------------------------------------------------- #
#
# The command=INIT|APPEND|FINALIZE form on POST /2/media/upload answered a real
# republish with a bare 400 ("One or more parameters to your request was
# invalid"). These pin the dedicated sub-path endpoints that replaced it.

def test_initialize_sends_json_with_a_numeric_total_bytes(api):
    """A form-encoded total_bytes is what the 400 was about."""
    _publish(asset_path="/a.jpg", asset_paths=None)
    init = api["uploads"][0]
    assert init["total_bytes"] == len(b"\xff\xd8\xffdata")
    assert isinstance(init["total_bytes"], int)
    assert init["media_category"] == "tweet_image"
    assert "command" not in init


def test_the_media_type_is_sniffed_from_the_bytes(api, monkeypatch):
    """A PNG announced as image/jpeg is rejected by initialize."""
    from aismm.platforms import twitter as tw

    monkeypatch.setattr(tw, "read_bytes", lambda p: b"\x89PNG\r\n\x1a\npixels")
    _publish(asset_path="/a.jpg", asset_paths=None)   # extension lies; bytes win
    assert api["uploads"][0]["media_type"] == "image/png"


def test_append_and_finalize_address_the_media_by_id(api):
    _publish(asset_path="/a.jpg", asset_paths=None)
    url, data = api["appends"][0]
    assert url.endswith("/media/upload/media1/append")
    assert data["segment_index"] == "0"
    assert api["finalizes"] == ["https://api.x.com/2/media/upload/media1/finalize"]


def test_a_large_asset_is_appended_in_segments(api, monkeypatch):
    from aismm.platforms import twitter as tw

    monkeypatch.setattr(tw, "read_bytes", lambda p: b"\xff\xd8\xff" + b"0" * (tw._CHUNK * 2))
    _publish(asset_path="/big.jpg", asset_paths=None)
    assert [d["segment_index"] for _u, d in api["appends"]] == ["0", "1", "2"]


def test_no_single_post_exceeds_280(api):
    account = Account(platform=PlatformName.twitter, handle="a", external_id="9")
    asyncio.run(_x().publish(access_token="t", account=account, caption="x" * 400,
                             asset_path="", media_kind="text"))
    assert all(len(t["text"]) <= 280 for t in api["tweets"])


# --- the engagement tools ------------------------------------------------------------ #

class _Store:
    def get_tokens(self, _account_id):
        return ("token", "")


def _state(platform=PlatformName.twitter):
    return {"account": Account(platform=platform, handle="a", external_id="9"),
            "store": _Store(), "instruction": Instruction(name="i"),
            "run": None, "assets": []}


def _tool(name, state=None):
    tools = build_tools(state or _state())
    return next((t for t in tools if getattr(t, "name", "") == name), None)


X_TOOLS = ["x_recent_posts", "x_mentions", "x_reply_to_post",
           "x_post_metrics", "x_profile", "x_delete_post"]


@pytest.mark.parametrize("name", X_TOOLS)
def test_the_tool_exists_on_an_x_run(name):
    assert _tool(name) is not None


@pytest.mark.parametrize("name", X_TOOLS)
def test_the_tool_is_absent_on_other_platforms(name):
    """An Instagram run must not be handed six irrelevant X tools."""
    assert _tool(name, _state(PlatformName.instagram)) is None


def test_instagram_tools_are_absent_on_an_x_run():
    assert _tool("instagram_recent_posts") is None


def test_recent_posts_reads_the_account(api):
    account = Account(platform=PlatformName.twitter, handle="a", external_id="9")
    posts = asyncio.run(_x().list_posts("t", account, limit=5))
    assert posts and posts[0]["id"] == "1"


def test_reply_posts_immediately(api):
    result = asyncio.run(_x().reply("t", "555", "thanks!"))
    assert result["id"] == "1799"
    assert api["tweets"][0]["reply"]["in_reply_to_tweet_id"] == "555"


def test_a_reply_is_also_held_to_280(api):
    asyncio.run(_x().reply("t", "555", "y" * 400))
    assert len(api["tweets"][0]["text"]) == 280


def test_delete_calls_the_delete_endpoint(api):
    asyncio.run(_x().delete_post("t", "555"))
    assert api["deletes"] and api["deletes"][0].endswith("/tweets/555")


def test_profile_returns_counts(api):
    data = asyncio.run(_x().profile("t"))
    assert data["public_metrics"]["followers_count"] == 42


# --- billing errors must be spelled out, not left as a bare status ------------------- #
# X moved to pay-per-use credits in Feb 2026 — there is no free tier. An account
# out of credits gets 402 on EVERYTHING, posting included, and httpx's own
# message ("Client error '402 Payment Required'") tells you nothing actionable.

def _failing_get(monkeypatch, status, payload):
    from aismm.platforms import twitter as tw

    class _Resp:
        status_code = status
        text = str(payload)

        def json(self):
            return payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(tw.httpx, "AsyncClient", lambda **kw: _Client())


def test_402_is_named_as_billing_and_points_at_the_console(monkeypatch):
    """The reported failure: the agent only saw "Client error '402 Payment Required'"
    and suggested "restore X publishing access/billing" — right by luck, not by
    being told. It must be unambiguous that no rewording of the post will help."""
    _failing_get(monkeypatch, 402, {"detail": "no credits"})
    account = Account(platform=PlatformName.twitter, external_id="9")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_x().list_posts("t", account))
    message = str(exc.value)
    assert "BILLING" in message
    assert "console.x.com" in message
    assert "no credits left" in message


def test_403_points_at_the_token_not_at_billing(monkeypatch):
    _failing_get(monkeypatch, 403, {"detail": "Unsupported Authentication"})
    account = Account(platform=PlatformName.twitter, external_id="9")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_x().list_posts("t", account))
    assert "token is rejected" in str(exc.value)
    assert "BILLING" not in str(exc.value)


def test_429_says_rate_limited(monkeypatch):
    _failing_get(monkeypatch, 429, {"detail": "Too Many Requests"})
    account = Account(platform=PlatformName.twitter, external_id="9")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_x().list_posts("t", account))
    assert "rate limited" in str(exc.value)


def test_the_api_error_carries_the_reason_not_just_a_status(monkeypatch):
    _failing_get(monkeypatch, 400, {"detail": "Invalid Request: max_results"})
    account = Account(platform=PlatformName.twitter, external_id="9")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_x().list_mentions("t", account))
    assert "max_results" in str(exc.value)


def test_api_error_includes_x_request_id_when_present():
    class _Response:
        status_code = 503
        text = "Service Unavailable"
        headers = {"x-request-id": "x-trace-123"}

        def json(self):
            return {"title": "Service Unavailable"}

    assert "x-trace-123" in str(_x()._api_error(_Response()))


def test_a_tool_reports_the_error_rather_than_killing_the_run(monkeypatch):
    """A 403 must come back as a result dict, not an exception that ends the run."""
    from aismm.tools import twitter_tools

    _failing_get(monkeypatch, 403, {"detail": "Unsupported Authentication"})

    async def call(platform, account, token):
        return await platform.list_posts(token, account)

    result = asyncio.run(twitter_tools._with_context(_state(), call))
    assert result["error"] == "x_api_error"
    assert "token is rejected" in result["message"]


def test_a_tool_on_the_wrong_platform_says_so():
    from aismm.tools import twitter_tools

    async def call(platform, account, token):
        raise AssertionError("should not be reached")

    result = asyncio.run(
        twitter_tools._with_context(_state(PlatformName.instagram), call))
    assert result["error"] == "not_available"


# --- the duplicate guard can ask X too ----------------------------------------------- #

def test_a_deleted_post_is_reported_as_gone(monkeypatch):
    _failing_get(monkeypatch, 200, {"errors": [{"title": "Not Found Error"}]})
    account = Account(platform=PlatformName.twitter, external_id="9")
    assert asyncio.run(_x().post_exists("t", account, "1")) is False


def test_a_live_post_is_reported_as_live(monkeypatch):
    _failing_get(monkeypatch, 200, {"data": {"id": "1"}})
    account = Account(platform=PlatformName.twitter, external_id="9")
    assert asyncio.run(_x().post_exists("t", account, "1")) is True


def test_a_403_is_unknown_not_deleted(monkeypatch):
    """The guard must not read "cannot check" as "was deleted"."""
    _failing_get(monkeypatch, 403, {"detail": "Unsupported Authentication"})
    account = Account(platform=PlatformName.twitter, external_id="9")
    assert asyncio.run(_x().post_exists("t", account, "1")) is None


# --- long captions become a thread, not a truncation --------------------------------- #
# `publish` used to do `caption[:280]`, so anything longer was silently cut
# mid-sentence. X's whole idiom for a long thought is a chain of linked posts.

from aismm.platforms.twitter import split_thread   # noqa: E402

LIMIT = 280


def test_a_short_caption_is_one_post_with_no_counter():
    assert split_thread("Just a thought.", LIMIT, 25) == ["Just a thought."]


def test_an_empty_caption_produces_nothing():
    assert split_thread("   ", LIMIT, 25) == []


def test_a_long_caption_splits_and_every_part_fits():
    text = ("Audiology matters. " * 60).strip()
    parts = split_thread(text, LIMIT, 25)
    assert len(parts) > 1
    assert all(len(p) <= LIMIT for p in parts), [len(p) for p in parts]


def test_parts_are_numbered_once_there_is_more_than_one():
    parts = split_thread("Audiology matters. " * 60, LIMIT, 25)
    assert parts[0].endswith(f" 1/{len(parts)}")
    assert parts[-1].endswith(f" {len(parts)}/{len(parts)}")


def test_no_word_is_split_in_half():
    text = "Supercalifragilistic expialidocious " * 40
    for part in split_thread(text, LIMIT, 25):
        body = part.rsplit(" ", 1)[0]           # drop the n/m counter
        for word in body.split():
            assert word in text


def test_it_prefers_to_break_at_a_paragraph():
    first = "A" * 200
    second = "B" * 200
    parts = split_thread(f"{first}\n\n{second}", LIMIT, 25)
    assert parts[0].startswith("A") and "B" not in parts[0]


def test_it_falls_back_to_a_sentence_boundary():
    text = ("First sentence here. " * 10) + ("Second part follows. " * 10)
    parts = split_thread(text, LIMIT, 25)
    # Every part should end on a sentence (before the counter), not mid-clause.
    assert parts[0].rsplit(" ", 1)[0].rstrip().endswith(".")


def test_the_whole_caption_survives_the_split():
    text = "Audiology matters and hearing health is underrated. " * 20
    joined = " ".join(p.rsplit(" ", 1)[0] for p in split_thread(text, LIMIT, 25))
    assert "underrated" in joined
    assert joined.count("Audiology") == text.count("Audiology")


def test_a_runaway_caption_is_bounded_by_max_posts():
    parts = split_thread("word " * 5000, LIMIT, 5)
    assert len(parts) == 5


# --- posting the thread -------------------------------------------------------------- #

def _long_publish(api, caption, **kwargs):
    account = Account(platform=PlatformName.twitter, handle="abo0zar", external_id="9")
    return asyncio.run(_x().publish(access_token="t", account=account, caption=caption,
                                    asset_path=kwargs.pop("asset_path", ""),
                                    media_kind=kwargs.pop("media_kind", "text"), **kwargs))


def test_each_part_replies_to_the_one_before(api):
    _long_publish(api, "Audiology matters. " * 60)
    assert len(api["tweets"]) > 1
    assert "reply" not in api["tweets"][0]
    for tweet in api["tweets"][1:]:
        assert tweet["reply"]["in_reply_to_tweet_id"] == "1799"


def test_the_result_points_at_the_FIRST_post(api):
    """That is the thread's permalink, and what the duplicate ledger records."""
    result = _long_publish(api, "Audiology matters. " * 60)
    assert result.external_id == "1799"
    assert result.url == "https://x.com/abo0zar/status/1799"
    assert result.raw["thread_posts"] == len(api["tweets"])


def test_media_rides_only_on_the_first_post(api):
    """Otherwise X repeats the image all the way down the thread."""
    _long_publish(api, "Audiology matters. " * 60,
                  asset_path="/a.jpg", media_kind="image")
    assert "media" in api["tweets"][0]
    assert all("media" not in t for t in api["tweets"][1:])


def test_a_short_caption_still_posts_exactly_once(api):
    _long_publish(api, "Short and sweet.")
    assert len(api["tweets"]) == 1
    assert "reply" not in api["tweets"][0]


def test_the_publish_gate_gives_x_the_whole_thread_budget():
    """caption_limit bounds ONE post; trimming to it would pre-truncate the thread."""
    caps = _x().capabilities
    assert caps.supports_threads is True
    assert caps.max_thread_posts > 1
    assert caps.caption_limit * caps.max_thread_posts > 1000


def test_platforms_that_cannot_thread_say_so():
    for name in (PlatformName.instagram, PlatformName.youtube, PlatformName.tiktok):
        assert registry.get_platform(name).capabilities.supports_threads is False


# --- the AI label must be on the post people actually see ---------------------------- #
# The disclosure is appended to the caption, so on a thread it lands on the LAST
# post — unseen by anyone who only meets post 1 in their timeline, which is the
# "first exposure" the label exists to cover.

LABEL = "🤖 AI-generated"


def test_the_label_moves_to_the_first_post_of_a_thread():
    text = ("Audiology matters. " * 60) + f"\n\n{LABEL}"
    parts = split_thread(text, LIMIT, 25, pin_suffix=LABEL)
    assert len(parts) > 1
    assert LABEL in parts[0]
    assert all(LABEL not in p for p in parts[1:])


def test_pinning_the_label_does_not_push_a_post_over_the_limit():
    text = ("Audiology matters. " * 60) + f"\n\n{LABEL}"
    assert all(len(p) <= LIMIT for p in split_thread(text, LIMIT, 25, pin_suffix=LABEL))


def test_a_single_post_keeps_the_label_at_the_end():
    parts = split_thread(f"Short one.\n\n{LABEL}", LIMIT, 25, pin_suffix=LABEL)
    assert parts == [f"Short one.\n\n{LABEL}"]


def test_no_label_is_invented_when_the_caption_has_none():
    parts = split_thread("Audiology matters. " * 60, LIMIT, 25, pin_suffix=LABEL)
    assert all(LABEL not in p for p in parts)


def test_a_caption_that_is_only_the_label_survives():
    assert split_thread(LABEL, LIMIT, 25, pin_suffix=LABEL) == [LABEL]


def test_the_first_posted_tweet_carries_the_label(api):
    account = Account(platform=PlatformName.twitter, handle="a", external_id="9")
    asyncio.run(_x().publish(
        access_token="t", account=account,
        caption=("Audiology matters. " * 60) + f"\n\n{LABEL}",
        asset_path="", media_kind="text"))
    assert LABEL in api["tweets"][0]["text"]
    assert all(LABEL not in t["text"] for t in api["tweets"][1:])


def test_a_402_while_POSTING_is_explained_too(monkeypatch):
    """The reported case was a publish, not a read — every X call path must explain it."""
    from aismm.platforms import twitter as tw

    class _Resp:
        status_code = 402
        text = "Payment Required"

        def json(self):
            return {"detail": "Your enrolled account does not have any credits"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(tw.httpx, "AsyncClient", lambda **kw: _Client())
    account = Account(platform=PlatformName.twitter, handle="a", external_id="9")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_x().publish(access_token="t", account=account, caption="hello",
                                 asset_path="", media_kind="text"))
    assert "BILLING" in str(exc.value)
    assert "console.x.com" in str(exc.value)


def test_the_specific_parameter_complaint_is_not_swallowed(monkeypatch):
    """The 400 that broke media upload said only "One or more parameters to your
    request was invalid" — X named the real problem in errors[], and reading
    detail alone reduced a precise complaint to a guessing game."""
    _failing_get(monkeypatch, 400, {
        "detail": "One or more parameters to your request was invalid.",
        "errors": [{"message": "total_bytes must be an integer"}]})
    account = Account(platform=PlatformName.twitter, external_id="9")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_x().list_posts("t", account))
    assert "total_bytes must be an integer" in str(exc.value)
    assert "One or more parameters" in str(exc.value)


def test_a_repeated_message_is_not_printed_twice(monkeypatch):
    _failing_get(monkeypatch, 400, {"detail": "bad id", "errors": [{"message": "bad id"}]})
    account = Account(platform=PlatformName.twitter, external_id="9")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_x().list_posts("t", account))
    assert str(exc.value).count("bad id") == 1
