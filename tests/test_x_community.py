"""The X destination on a connection: which community, and who else sees it.

A community post is visible only inside that community. X's own composer puts an
"Also share with followers" switch beside the community picker, and without the
equivalent here a post aimed at growing an audience reaches a room instead.
"""
import dataclasses

import pytest

from aismm import config as config_module
from aismm.config import AuthSettings
from aismm.dashboard import app as app_module
from aismm.dashboard import sso
from aismm.models import Account, PlatformName


@pytest.fixture()
def dash(store, monkeypatch, tmp_path):
    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


@pytest.fixture()
def account(store):
    return store.upsert_account(
        Account(platform=PlatformName.twitter, handle="abo0zar", external_id="9"),
        access_token="t")


def _save(dash, account, **form):
    return dash.test_client().post(f"/accounts/{account.id}/community", data=form,
                                   follow_redirects=True)


def test_the_checkbox_is_on_the_connection(dash, account):
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert 'name="share_with_followers"' in page
    assert "Also share with followers" in page


def test_it_is_saved_with_the_community(dash, store, account):
    _save(dash, account, community_id="123", share_with_followers="on")
    saved = store.get_account(account.id)
    assert saved.meta["community_id"] == "123"
    assert saved.meta["share_with_followers"] is True


def test_it_can_be_turned_off(dash, store, account):
    _save(dash, account, community_id="123", share_with_followers="on")
    _save(dash, account, community_id="123")          # unticked = absent from the form
    assert store.get_account(account.id).meta["share_with_followers"] is False


def test_it_shows_as_ticked_once_saved(dash, store, account):
    _save(dash, account, community_id="123", share_with_followers="on")
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "checked" in page


def test_clearing_the_community_clears_the_flag_too(dash, store, account):
    """Otherwise it silently applies to whichever community is set next."""
    _save(dash, account, community_id="123", share_with_followers="on")
    _save(dash, account, community_id="")
    meta = store.get_account(account.id).meta
    assert "community_id" not in meta
    assert "share_with_followers" not in meta


def test_the_confirmation_says_who_will_see_the_posts(dash, account):
    page = _save(dash, account, community_id="123").get_data(as_text=True)
    assert "followers will not see them" in page
    page = _save(dash, account, community_id="123",
                 share_with_followers="on").get_data(as_text=True)
    assert "and to your followers" in page


def test_a_non_numeric_community_is_refused(dash, store, account):
    page = _save(dash, account, community_id="not-an-id").get_data(as_text=True)
    assert "digits only" in page
    assert "community_id" not in store.get_account(account.id).meta


def test_the_form_is_only_shown_for_x(dash, store):
    store.upsert_account(Account(platform=PlatformName.instagram, handle="ig",
                                 external_id="1"), access_token="t")
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert page.count('name="share_with_followers"') == 0


def test_it_cannot_be_set_on_another_platforms_account(dash, store):
    other = store.upsert_account(Account(platform=PlatformName.instagram, external_id="1"),
                                 access_token="t")
    assert dash.test_client().post(f"/accounts/{other.id}/community",
                                   data={"community_id": "123"}).status_code == 404


# --- rotating through several communities -------------------------------------------- #
# Rotation, not fan-out. Posting the same content to every community at once is
# several near-identical posts from one account within seconds, which is what X's
# duplicate-content rule describes — and on a pay-per-use API it multiplies the
# cost. A scheduler covers every community anyway, with different content each run.

def _rotate(store, account, times):
    """Publish `times` times, returning the community each post went to."""
    from aismm.platforms.registry import get_platform
    from aismm.platforms.twitter import next_community

    platform = get_platform(PlatformName.twitter)
    went_to = []
    for _ in range(times):
        current = store.get_account(account.id)
        went_to.append(next_community(current))
        platform.after_publish(account=current, store=store, result=None)
    return went_to


def test_several_communities_are_visited_in_turn(dash, store, account):
    _save(dash, account, community_id="111, 222, 333")
    assert _rotate(store, account, 6) == ["111", "222", "333", "111", "222", "333"]


def test_one_community_never_moves(dash, store, account):
    _save(dash, account, community_id="111")
    assert _rotate(store, account, 3) == ["111", "111", "111"]


def test_no_community_is_the_home_timeline(store, account):
    assert _rotate(store, account, 2) == ["", ""]


def test_the_whole_thread_goes_to_ONE_community(dash, store, account, monkeypatch):
    """Its later posts belong to the same community as the first."""
    from aismm.platforms import twitter as tw

    posts = []

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": {"id": f"18{len(posts)}"}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            posts.append(kw.get("json") or {})
            return _Resp()

    monkeypatch.setattr(tw.httpx, "AsyncClient", lambda **kw: _Client())
    _save(dash, account, community_id="111, 222")
    import asyncio

    from aismm.platforms.registry import get_platform

    asyncio.run(get_platform(PlatformName.twitter).publish(
        access_token="t", account=store.get_account(account.id), caption="x" * 700,
        asset_path="", media_kind="text", asset_paths=None))
    assert len(posts) > 1
    assert {post["community_id"] for post in posts} == {"111"}


def test_the_rotation_only_advances_on_a_LIVE_post(dash, store, account):
    """after_publish runs once the post has landed; advancing on a failure would
    silently skip a community for a whole cycle."""
    from aismm.platforms.twitter import next_community

    _save(dash, account, community_id="111, 222, 333")
    assert next_community(store.get_account(account.id)) == "111"
    assert next_community(store.get_account(account.id)) == "111"   # no publish, no move


def test_changing_the_list_restarts_the_rotation(dash, store, account):
    _save(dash, account, community_id="111, 222, 333")
    _rotate(store, account, 2)                       # cursor now at 333
    _save(dash, account, community_id="444, 555")
    assert _rotate(store, account, 1) == ["444"]


def test_resaving_the_same_list_does_not_restart_it(dash, store, account):
    """Flipping the followers switch must not send the next post back to the first."""
    _save(dash, account, community_id="111, 222, 333")
    _rotate(store, account, 1)                       # next is 222
    _save(dash, account, community_id="111, 222, 333", share_with_followers="on")
    assert _rotate(store, account, 1) == ["222"]


def test_the_list_survives_whitespace_and_separators(dash, store, account):
    _save(dash, account, community_id=" 111,222 ;  333 ")
    assert store.get_account(account.id).meta["community_ids"] == ["111", "222", "333"]


def test_a_repeated_id_is_only_listed_once(dash, store, account):
    _save(dash, account, community_id="111, 111, 222")
    assert store.get_account(account.id).meta["community_ids"] == ["111", "222"]


def test_one_bad_id_rejects_the_whole_list(dash, store, account):
    page = _save(dash, account, community_id="111, nope, 222").get_data(as_text=True)
    assert "nope" in page
    assert "community_ids" not in store.get_account(account.id).meta


def test_a_single_id_saved_by_an_older_version_still_works(store, account):
    """Existing connections stored community_id, not a list."""
    from aismm.platforms.twitter import community_ids, next_community

    account.set_meta({"community_id": "999"})
    store.upsert_account(account)
    stored = store.get_account(account.id)
    assert community_ids(stored) == ["999"]
    assert next_community(stored) == "999"


def test_clearing_the_list_clears_the_cursor(dash, store, account):
    _save(dash, account, community_id="111, 222")
    _rotate(store, account, 1)
    _save(dash, account, community_id="")
    meta = store.get_account(account.id).meta
    assert not any(key in meta for key in ("community_ids", "community_id",
                                           "community_cursor", "share_with_followers"))


def test_the_page_says_where_the_next_post_goes(dash, store, account):
    _save(dash, account, community_id="111, 222, 333")
    _rotate(store, account, 1)
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "3 communities, in rotation" in page
    assert "The next post goes to" in page
    # Named when we know the name, the bare id when we do not — never nothing.
    assert "<strong>222</strong>" in page


def test_saving_several_says_they_rotate(dash, store, account):
    page = _save(dash, account, community_id="111, 222").get_data(as_text=True)
    assert "rotate through 2 communities, one per run" in page
