"""X Premium accounts post up to 25,000 characters in ONE post.

Reported as "when I post myself the allowed length is much higher than what AISM2
uses". The limit was a fixed 280 for every X account, so a Premium account had its
long text split into a thread it never needed, each part a billed request.
"""
import asyncio
import dataclasses

import pytest

from aismm.models import Account, Instruction, PlatformName
from aismm.platforms import registry
from aismm.platforms import twitter as tw
from tests.test_twitter_surface import api  # noqa: F401  (the X API fake)


def _x():
    return registry.get_platform(PlatformName.twitter)


def _account(**meta):
    account = Account(platform=PlatformName.twitter, handle="abo0zar", external_id="9")
    account.set_meta(meta)
    return account


# --- who counts as Premium ------------------------------------------------------- #

@pytest.mark.parametrize("subscription,expected", [
    ("Premium", True), ("PremiumPlus", True), ("Basic", True),
    ("None", False), ("", False),
])
def test_every_paid_tier_gets_long_posts(subscription, expected):
    assert tw.long_posts(_account(x_subscription=subscription)) is expected


def test_the_operator_can_override_what_x_says():
    assert tw.long_posts(_account(x_subscription="None", x_long_posts="yes")) is True
    assert tw.long_posts(_account(x_subscription="Premium", x_long_posts="no")) is False
    assert tw.long_posts(_account()) is False            # never asked: stay safe at 280


# --- the limit is per account ------------------------------------------------------ #

def test_capabilities_for_a_premium_account():
    caps = _x().capabilities_for(_account(x_subscription="Premium"))
    assert caps.caption_limit == tw.LONG_POST_LIMIT == 25_000
    assert "Premium" in caps.notes
    assert _x().capabilities.caption_limit == 280        # the platform default is untouched


def test_other_accounts_and_platforms_are_unchanged():
    assert _x().capabilities_for(_account()) is _x().capabilities
    ig = registry.get_platform(PlatformName.instagram)
    assert ig.capabilities_for(Account(platform=PlatformName.instagram)) is ig.capabilities


def test_a_premium_account_posts_one_long_post(api):  # noqa: F811
    text = "word " * 400                                   # 2000 characters
    asyncio.run(_x().publish(access_token="t", account=_account(x_subscription="Premium"),
                             caption=text, asset_path="", media_kind="text",
                             instruction=Instruction(name="I", disclose_ai=False)))
    assert len(api["tweets"]) == 1
    assert api["tweets"][0]["text"] == text.strip()


def test_without_premium_the_same_text_is_still_a_thread(api):  # noqa: F811
    asyncio.run(_x().publish(access_token="t", account=_account(), caption="word " * 400,
                             asset_path="", media_kind="text",
                             instruction=Instruction(name="I", disclose_ai=False)))
    assert len(api["tweets"]) > 1
    assert all(len(t["text"]) <= 280 for t in api["tweets"])


def test_a_premium_reply_is_not_cut_at_280(api):  # noqa: F811
    asyncio.run(_x().reply("t", "555", "y" * 400, _account(x_subscription="Premium")))
    assert len(api["tweets"][0]["text"]) == 400


def test_the_agent_is_told_the_real_limit():
    from aismm.agent import prompts

    kickoff = prompts.build_kickoff(account=_account(x_subscription="Premium"),
                                    instruction=Instruction(name="I", brief="b"),
                                    platform_caps=_x().capabilities_for(
                                        _account(x_subscription="Premium")))
    assert "caption limit: 25000" in kickoff


# --- detection at connect ------------------------------------------------------------ #

def test_connect_asks_x_for_the_subscription_and_records_it(api):  # noqa: F811
    original = api  # the fake answers /users/me without the field: nothing is recorded
    identity = asyncio.run(_x().fetch_identity("t"))
    assert identity.meta == {}
    url, params = original["gets"][-1]
    assert "/users/me" in url and params.get("user.fields") == "subscription_type"


def test_a_reported_subscription_lands_in_meta(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": {"id": "9", "username": "abo0zar", "subscription_type": "Premium"}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return _Resp()

    monkeypatch.setattr(tw.httpx, "AsyncClient", lambda **kw: _Client())
    identity = asyncio.run(_x().fetch_identity("t"))
    assert identity.meta == {tw.SUBSCRIPTION_KEY: "Premium"}


# --- the Accounts page ---------------------------------------------------------------- #

@pytest.fixture()
def client(store, monkeypatch, tmp_path):
    from aismm import assets as assets_module
    from aismm import config as config_module
    from aismm.config import AuthSettings
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    (tmp_path / "assets").mkdir(exist_ok=True)
    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module, assets_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    store.init()
    application = app_module.create_app()
    application.secret_key = "test"
    return application.test_client()


def test_the_accounts_page_says_which_mode_an_account_is_in(client, store):
    store.upsert_account(_account(x_subscription="Premium"))
    page = client.get("/accounts").get_data(as_text=True)
    assert "one long post (up to 25,000 characters)" in page
    assert "it says Premium" in page


def test_the_operator_choice_is_saved_and_can_be_cleared(client, store):
    account = store.upsert_account(_account(x_subscription="None"))
    client.post(f"/accounts/{account.id}/x-long-posts", data={"x_long_posts": "yes"})
    assert tw.long_posts(store.get_account(account.id)) is True
    client.post(f"/accounts/{account.id}/x-long-posts", data={"x_long_posts": ""})
    saved = store.get_account(account.id)
    assert tw.LONG_POSTS_KEY not in saved.meta and tw.long_posts(saved) is False


def test_check_now_records_what_x_reports(client, store, monkeypatch):
    from aismm.dashboard import app as app_module
    from aismm.platforms.base import Identity

    account = store.upsert_account(_account())
    monkeypatch.setattr(app_module.tokens, "valid_access_token_sync", lambda a, s: "t")

    async def fake_identity(self, token):
        return Identity(external_id="9", handle="abo0zar",
                        meta={tw.SUBSCRIPTION_KEY: "PremiumPlus"})

    monkeypatch.setattr(tw.Twitter, "fetch_identity", fake_identity)
    client.post(f"/accounts/{account.id}/x-premium/check")
    saved = store.get_account(account.id)
    assert saved.meta[tw.SUBSCRIPTION_KEY] == "PremiumPlus" and tw.long_posts(saved)
