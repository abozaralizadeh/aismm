"""Instagram beyond a single feed post: carousels, stories, and engagement.

Graph is mocked throughout — these pin the request shapes (which is where the
API's rules live) and the per-platform tool gating.
"""
import asyncio
import json

import httpx
import pytest

from aismm.models import Account, Instruction, PlatformName, PublishMode, Run
from aismm.platforms.instagram import Instagram
from aismm.platforms.registry import get_platform
from aismm.tools import instagram_tools
from aismm.tools.publish_tool import perform_publish
from aismm.tools.registry import build_tools

IG_USER = "17841400000000000"


@pytest.fixture()
def account(store):
    return store.upsert_account(
        Account(platform=PlatformName.instagram, handle="brand", external_id=IG_USER),
        access_token="page-token")


def _graph(handler):
    """Patch httpx.AsyncClient inside the platform module with a mock transport."""
    requests: list[httpx.Request] = []

    def record(request):
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    real = httpx.AsyncClient
    return (lambda *a, **kw: real(*a, **{**kw, "transport": transport})), requests


def _form(request) -> dict:
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# --- publishing: carousel + stories ------------------------------------------------- #

def _publish_handler(container_ids):
    """Answer container creation, status polls, publish, and permalink."""
    created = iter(container_ids)

    def handler(request):
        path, method = request.url.path, request.method
        if "graph.facebook.com" not in str(request.url):
            return httpx.Response(200, headers={"content-type": "image/jpeg"})
        if path.endswith("/media") and method == "POST":
            return httpx.Response(200, json={"id": next(created)})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "media-final"})
        if "insights" in path:
            return httpx.Response(200, json={"data": []})
        if method == "GET" and any(cid in path for cid in container_ids):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        return httpx.Response(200, json={"permalink": "https://instagram.com/p/x"})

    return handler


def _run_publish(monkeypatch, tmp_path, *, paths, placement="feed", caption="hi"):
    from aismm.platforms import instagram as ig_module

    for index, path in enumerate(paths):
        (tmp_path / path).write_bytes(b"\xff\xd8\xffdata")
    client, requests = _graph(_publish_handler([f"container-{i}" for i in range(len(paths) + 2)]))
    monkeypatch.setattr(ig_module.httpx, "AsyncClient", client)
    monkeypatch.setattr(ig_module, "public_url", lambda p: f"https://cdn.example.com/{p}")

    result = asyncio.run(Instagram(creds=None).publish(
        access_token="tok", account=Account(platform=PlatformName.instagram,
                                            external_id=IG_USER),
        caption=caption, asset_path=str(tmp_path / paths[0]), media_kind="image",
        asset_paths=[str(tmp_path / p) for p in paths], placement=placement))
    return result, requests


def test_single_image_still_posts_as_before(monkeypatch, tmp_path):
    result, requests = _run_publish(monkeypatch, tmp_path, paths=["a.jpg"])
    creates = [r for r in requests if r.url.path.endswith("/media") and r.method == "POST"]
    assert len(creates) == 1
    body = _form(creates[0])
    assert body["image_url"].endswith("a.jpg")
    assert "media_type" not in body            # a plain feed image has none
    assert result.external_id == "media-final"


def test_several_images_become_a_carousel(monkeypatch, tmp_path):
    """Children carry is_carousel_item; the parent lists them and holds the caption."""
    _result, requests = _run_publish(monkeypatch, tmp_path,
                                     paths=["a.jpg", "b.jpg", "c.jpg"], caption="three")
    creates = [_form(r) for r in requests
               if r.url.path.endswith("/media") and r.method == "POST"]
    children, parent = creates[:-1], creates[-1]

    assert len(children) == 3
    assert all(c["is_carousel_item"] == "true" for c in children)
    assert all("caption" not in c for c in children)      # caption belongs to the parent
    assert parent["media_type"] == "CAROUSEL"
    assert len(parent["children"].split(",")) == 3
    assert parent["caption"] == "three"


def test_a_story_uses_the_stories_media_type_and_no_caption(monkeypatch, tmp_path):
    """Stories take no caption — Graph has nowhere to put it."""
    _result, requests = _run_publish(monkeypatch, tmp_path, paths=["a.jpg"],
                                     placement="story", caption="ignored")
    body = _form([r for r in requests
                  if r.url.path.endswith("/media") and r.method == "POST"][0])
    assert body["media_type"] == "STORIES"
    assert "caption" not in body


def test_a_video_carousel_child_uses_VIDEO_not_REELS(monkeypatch, tmp_path):
    """REELS is a standalone placement; inside a carousel the child is VIDEO."""
    _result, requests = _run_publish(monkeypatch, tmp_path, paths=["a.mp4", "b.jpg"])
    children = [_form(r) for r in requests
                if r.url.path.endswith("/media") and r.method == "POST"][:-1]
    video_child = [c for c in children if "video_url" in c][0]
    assert video_child["media_type"] == "VIDEO"


# --- the publish gate validates placements ------------------------------------------- #

def _gate(store, account, **kwargs):
    instruction = store.upsert_instruction(Instruction(name="i",
                                                       publish_mode=PublishMode.dry_run))
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id))
    state = {"account": account, "instruction": instruction, "store": store, "run": run,
             "assets": []}
    return asyncio.run(perform_publish(state, "caption", **kwargs)), state


def test_gate_rejects_a_carousel_on_a_platform_without_one(store, monkeypatch):
    """Stubbed capabilities, not a real platform: this is about the GATE.

    It used to use X as the no-carousel example, which quietly stopped testing
    anything the day X gained 4-image posts.
    """
    import dataclasses

    from aismm.platforms import registry

    twitter = store.upsert_account(Account(platform=PlatformName.twitter, external_id="1"),
                                   access_token="t")
    platform = registry.get_platform(PlatformName.twitter)
    monkeypatch.setattr(platform, "capabilities",
                        dataclasses.replace(platform.capabilities, supports_carousel=False))
    monkeypatch.setattr(registry, "get_platform", lambda *a, **kw: platform)

    result, _ = _gate(store, twitter, asset_paths=["/a.jpg", "/b.jpg"], media_kind="image")
    assert result["error"] == "unsupported_placement"


def test_x_accepts_four_images_but_not_five(store):
    """X's real limit, enforced by the gate before any upload happens."""
    twitter = store.upsert_account(Account(platform=PlatformName.twitter, external_id="1"),
                                   access_token="t")
    five = [f"/{n}.jpg" for n in range(5)]
    result, _ = _gate(store, twitter, asset_paths=five, media_kind="image")
    assert result["error"] == "too_many_items"
    assert "4" in result["message"]


def test_gate_rejects_a_story_on_a_platform_without_one(store):
    twitter = store.upsert_account(Account(platform=PlatformName.twitter, external_id="1"),
                                   access_token="t")
    result, _ = _gate(store, twitter, asset_path="/a.jpg", media_kind="image",
                      placement="story")
    assert result["error"] == "unsupported_placement"


def test_gate_enforces_the_carousel_item_cap(store, account):
    result, _ = _gate(store, account, media_kind="image",
                      asset_paths=[f"/{i}.jpg" for i in range(12)])
    assert result["error"] == "too_many_items"
    assert "10" in result["message"]


def test_gate_rejects_an_unknown_placement(store, account):
    result, _ = _gate(store, account, asset_path="/a.jpg", media_kind="image",
                      placement="billboard")
    assert result["error"] == "unknown_placement"


def test_staged_post_remembers_every_item_and_the_placement(store, account, tmp_path,
                                                            monkeypatch):
    from aismm import assets, config as config_module
    import dataclasses

    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(b"\xff\xd8\xffx")
    _gate(store, account, media_kind="image",
          asset_paths=[str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")])
    staged = store.list_staged()[0]
    assert len(staged.asset_paths) == 2
    assert staged.placement == "feed"


# --- engagement tools ---------------------------------------------------------------- #

def _tool_state(store, account):
    return {"account": account, "store": store,
            "instruction": Instruction(name="i"), "assets": []}


def test_instagram_tools_only_appear_for_instagram_runs(store, account):
    tiktok = store.upsert_account(Account(platform=PlatformName.tiktok, external_id="2"),
                                  access_token="t")
    ig_names = {t.name for t in build_tools(_tool_state(store, account))}
    tt_names = {t.name for t in build_tools(_tool_state(store, tiktok))}

    assert "instagram_recent_posts" in ig_names
    assert "instagram_reply_to_comment" in ig_names
    # The cross-post sweep is what lets one engage run answer comments on every
    # recent post and reel, not just the latest one.
    assert "instagram_recent_comments" in ig_names
    assert not any(n.startswith("instagram_") for n in tt_names)


def test_every_instagram_tool_is_registered():
    from aismm.tools.registry import registered_tool_names

    names = registered_tool_names()
    for expected in ("instagram_recent_posts", "instagram_comments",
                     "instagram_recent_comments", "instagram_reply_to_comment",
                     "instagram_dms", "instagram_reply_to_dm",
                     "instagram_moderate_comment", "instagram_insights",
                     "instagram_publishing_limit", "instagram_profile",
                     "instagram_mentions"):
        assert expected in names


def _call_platform(monkeypatch, handler, coro_factory):
    from aismm.platforms import instagram as ig_module

    client, requests = _graph(handler)
    monkeypatch.setattr(ig_module.httpx, "AsyncClient", client)
    return asyncio.run(coro_factory(Instagram(creds=None))), requests


def test_recent_posts_requests_captions_and_counts(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"id": "1", "caption": "hello", "permalink": "https://p/1",
             "like_count": 5, "comments_count": 2}]})

    posts, requests = _call_platform(
        monkeypatch, handler,
        lambda p: p.list_media("tok", Account(platform=PlatformName.instagram,
                                              external_id=IG_USER), limit=5))
    assert posts[0]["caption"] == "hello"
    fields = requests[0].url.params["fields"]
    assert "caption" in fields and "like_count" in fields and "permalink" in fields


def test_comments_include_replies(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"id": "c1", "text": "nice", "username": "someone",
             "replies": {"data": [{"id": "r1", "text": "thanks"}]}}]})

    comments, requests = _call_platform(
        monkeypatch, handler, lambda p: p.list_comments("tok", "media-1"))
    assert comments[0]["text"] == "nice"
    assert "replies" in requests[0].url.params["fields"]


def test_reply_posts_to_the_replies_edge(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"id": "reply-1"})

    result, requests = _call_platform(
        monkeypatch, handler, lambda p: p.reply_to_comment("tok", "c1", "thank you!"))
    assert result["id"] == "reply-1"
    assert requests[0].url.path.endswith("/c1/replies")
    assert _form(requests[0])["message"] == "thank you!"


def test_hiding_a_comment_uses_the_hide_field(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"success": True})

    _result, requests = _call_platform(
        monkeypatch, handler, lambda p: p.set_comment_hidden("tok", "c1", hidden=True))
    assert _form(requests[0])["hide"] == "true"


def test_deleting_a_comment_uses_DELETE(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"success": True})

    _result, requests = _call_platform(
        monkeypatch, handler, lambda p: p.delete_comment("tok", "c1"))
    assert requests[0].method == "DELETE"


def test_publishing_limit_reports_what_is_left(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"quota_usage": 23, "config": {"quota_total": 100, "quota_duration": 86400}}]})

    from aismm.platforms import instagram as ig_module

    client, _requests = _graph(handler)
    monkeypatch.setattr(ig_module.httpx, "AsyncClient", client)
    account = Account(platform=PlatformName.instagram, external_id=IG_USER)
    limit = asyncio.run(Instagram(creds=None).publishing_limit("tok", account))
    assert limit["quota_usage"] == 23 and limit["config"]["quota_total"] == 100


def test_insights_default_metrics_avoid_deprecated_names():
    """v21 retired impressions/profile_views — don't ask for them by default."""
    for metric in ("impressions", "profile_views", "website_clicks", "video_views"):
        assert metric not in Instagram.DEFAULT_MEDIA_METRICS
        assert metric not in Instagram.DEFAULT_ACCOUNT_METRICS


def test_publishing_scopes_are_always_requested():
    """Whatever else changes, these are what a post needs."""
    scopes = get_platform(PlatformName.instagram).scopes
    for required in Instagram.REQUIRED_SCOPES:
        assert required in scopes


def test_comment_scopes_are_requested():
    assert "instagram_manage_comments" in get_platform(PlatformName.instagram).scopes


def test_insights_is_requested_by_default():
    """Deliberate: the default asks for the full set, insights included.

    Meta rejects the WHOLE dialog on one unavailable scope, so an app that has
    not been granted `instagram_manage_insights` gets "Invalid Scopes: …" and
    cannot connect at all — the escape hatch is INSTAGRAM_SCOPES, tested below.
    """
    assert "instagram_manage_insights" in get_platform(PlatformName.instagram).scopes


def test_a_publish_only_app_can_strip_the_review_gated_scopes(monkeypatch):
    """The recovery path when the dialog refuses: ask for less."""
    import dataclasses

    from aismm import config as config_module

    monkeypatch.setattr(config_module, "settings", dataclasses.replace(
        config_module.settings, instagram_scopes=" ".join(Instagram.REQUIRED_SCOPES)))
    scopes = get_platform(PlatformName.instagram).scopes
    assert "instagram_manage_insights" not in scopes
    assert "instagram_content_publish" in scopes      # can still publish


def test_the_scope_override_accepts_commas_or_spaces(monkeypatch):
    import dataclasses

    from aismm import config as config_module

    monkeypatch.setattr(config_module, "settings", dataclasses.replace(
        config_module.settings, instagram_scopes="instagram_basic, pages_show_list"))
    assert get_platform(PlatformName.instagram).scopes == ["instagram_basic", "pages_show_list"]


def test_capabilities_advertise_the_new_placements():
    caps = get_platform(PlatformName.instagram).capabilities
    assert caps.supports_carousel and caps.supports_stories
    assert caps.supports_comments and caps.supports_insights
    assert caps.max_carousel_items == 10


def test_a_tool_without_a_token_reports_rather_than_crashing(store):
    """An account whose token was revoked must not blow up the run."""
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id="x"), access_token="")
    state = _tool_state(store, account)
    assert asyncio.run(instagram_tools._instagram_context(state)) is None


# --- the cross-post comment sweep (one engage run answers every post) ---------------- #

def _invoke(tool, **kwargs):
    """Drive a function_tool from a test, as the agent runtime would."""
    from agents import RunConfig
    from agents.tool_context import ToolContext

    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="1",
                      tool_arguments="{}", run_config=RunConfig())
    return asyncio.run(tool.on_invoke_tool(ctx, json.dumps(kwargs)))


def _sweep_tool(store, account):
    return next(t for t in build_tools(_tool_state(store, account))
                if t.name == "instagram_recent_comments")


def _sweep_handler(request):
    path = request.url.path
    if path.endswith("/media"):
        return httpx.Response(200, json={"data": [
            {"id": "m1", "media_product_type": "FEED", "permalink": "https://p/1"},
            {"id": "m2", "media_product_type": "REELS", "permalink": "https://p/2"}]})
    if path.endswith("m1/comments"):
        return httpx.Response(200, json={"data": [
            {"id": "c1", "text": "on the post", "username": "a"}]})
    if path.endswith("m2/comments"):
        return httpx.Response(200, json={"data": [
            {"id": "c2", "text": "on the reel", "username": "b"}]})
    return httpx.Response(200, json={"data": []})


def test_the_sweep_reads_comments_across_every_recent_post(monkeypatch, store, account):
    from aismm.platforms import instagram as ig_module

    client, _ = _graph(_sweep_handler)
    monkeypatch.setattr(ig_module.httpx, "AsyncClient", client)

    result = _invoke(_sweep_tool(store, account), posts=10)
    assert result["scanned_media"] == 2
    ids = {c["id"] for c in result["comments"]}
    assert ids == {"c1", "c2"}                      # the reel comment is NOT missed
    by_id = {c["id"]: c for c in result["comments"]}
    assert by_id["c2"]["media_id"] == "m2"
    assert by_id["c2"]["media_type"] == "REELS"


def test_the_sweep_flags_already_answered_and_counts_pending(monkeypatch, store, account):
    from aismm.platforms import instagram as ig_module

    client, _ = _graph(_sweep_handler)
    monkeypatch.setattr(ig_module.httpx, "AsyncClient", client)
    monkeypatch.setattr(instagram_tools.engagement_ledger, "answered",
                        lambda acc, kind, cid: cid == "c1")

    result = _invoke(_sweep_tool(store, account), posts=10)
    assert result["count"] == 2 and result["pending"] == 1
    answered = {c["id"]: c["already_answered"] for c in result["comments"]}
    assert answered == {"c1": True, "c2": False}


def test_one_post_failing_does_not_lose_the_others(monkeypatch, store, account):
    from aismm.platforms import instagram as ig_module

    def handler(request):
        if request.url.path.endswith("m1/comments"):
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return _sweep_handler(request)

    client, _ = _graph(handler)
    monkeypatch.setattr(ig_module.httpx, "AsyncClient", client)

    result = _invoke(_sweep_tool(store, account), posts=10)
    # m1's comments failed, but m2's still come back.
    assert {c["id"] for c in result["comments"]} == {"c2"}


# --- media must exist, and must come from THIS run ---------------------------------- #

def test_declaring_a_kind_without_a_file_is_rejected(store, account):
    """The live failure: media_kind="image" with items=0 reached Instagram."""
    result, _ = _gate(store, account, media_kind="image")
    assert result["error"] == "no_media_attached"
    assert "generate_image" in result["message"]
    assert "previous run" in result["message"]


def test_a_video_kind_without_a_file_names_the_video_tool(store, account):
    result, _ = _gate(store, account, media_kind="video")
    assert result["error"] == "no_media_attached"
    assert "generate_video" in result["message"]


def test_text_only_on_a_media_platform_still_reports_the_media_requirement(store, account):
    result, _ = _gate(store, account, media_kind="text")
    assert result["error"] == "unsupported_media"
    assert "requires media" in result["message"]


def test_a_remembered_path_that_no_longer_exists_is_rejected(store, account):
    """Reusing an asset path from an earlier run is the mistake that caused this."""
    result, _ = _gate(store, account, asset_path="/gone/from-last-run.jpg",
                      media_kind="image")
    assert result["error"] == "asset_missing"
    assert "from-last-run.jpg" in result["message"]
    assert "not carried over between runs" in result["message"]


def test_a_carousel_with_one_missing_item_is_rejected(store, account, tmp_path):
    real = tmp_path / "there.jpg"
    real.write_bytes(b"\xff\xd8\xffx")
    result, _ = _gate(store, account, media_kind="image",
                      asset_paths=[str(real), "/gone/missing.jpg"])
    assert result["error"] == "asset_missing"
    assert "1 of 2" in result["message"]


def test_nothing_is_staged_when_the_media_check_fails(store, account):
    _gate(store, account, media_kind="image")
    assert store.list_staged() == []


def test_placement_errors_still_win_over_the_existence_check(store, account):
    """A wrong placement is a clearer message than 'asset missing' when both apply."""
    result, _ = _gate(store, account, asset_path="/nope.jpg", media_kind="image",
                      placement="billboard")
    assert result["error"] == "unknown_placement"
