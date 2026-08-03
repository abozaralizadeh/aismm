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

    calls = {"uploads": [], "tweets": [], "gets": [], "deletes": []}

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
            if url.endswith("/media/upload"):
                data = kw.get("data", {})
                if data.get("command") == "INIT":
                    calls["uploads"].append(data)
                    return _Resp({"data": {"id": f"media{len(calls['uploads'])}"}})
                return _Resp({"data": {"id": "media"}})
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


def test_the_caption_is_held_to_280(api):
    account = Account(platform=PlatformName.twitter, handle="a", external_id="9")
    asyncio.run(_x().publish(access_token="t", account=account, caption="x" * 400,
                             asset_path="", media_kind="text"))
    assert len(api["tweets"][0]["text"]) == 280


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


# --- the Free tier is write-only, and must say so ------------------------------------ #

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


def test_a_403_explains_the_access_tier(monkeypatch):
    """"No posts" and "your plan cannot read posts" need different responses."""
    _failing_get(monkeypatch, 403, {"detail": "Unsupported Authentication"})
    account = Account(platform=PlatformName.twitter, external_id="9")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_x().list_posts("t", account))
    assert "Free tier is write-only" in str(exc.value)
    assert "Basic plan" in str(exc.value)


def test_the_api_error_carries_the_reason_not_just_a_status(monkeypatch):
    _failing_get(monkeypatch, 400, {"detail": "Invalid Request: max_results"})
    account = Account(platform=PlatformName.twitter, external_id="9")
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_x().list_mentions("t", account))
    assert "max_results" in str(exc.value)


def test_a_tool_reports_the_error_rather_than_killing_the_run(monkeypatch):
    """A 403 must come back as a result dict, not an exception that ends the run."""
    from aismm.tools import twitter_tools

    _failing_get(monkeypatch, 403, {"detail": "Unsupported Authentication"})

    async def call(platform, account, token):
        return await platform.list_posts(token, account)

    result = asyncio.run(twitter_tools._with_context(_state(), call))
    assert result["error"] == "x_api_error"
    assert "Free tier" in result["message"]


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
    """On the Free tier the guard must not read 'cannot check' as 'was deleted'."""
    _failing_get(monkeypatch, 403, {"detail": "Unsupported Authentication"})
    account = Account(platform=PlatformName.twitter, external_id="9")
    assert asyncio.run(_x().post_exists("t", account, "1")) is None
