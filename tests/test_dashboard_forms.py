"""The forms an operator types into, and what the pages show them back.

Five reported problems, each pinned here:
  1. an Instagram-only instruction still offered a YouTube visibility picker and
     an X community card — settings that can never take effect;
  2. "Enabled" and "Next run" were flat text, and no column could be sorted;
  3. X communities were one textarea holding ``ID = Name`` per line, and the
     Sora pool three comma-separated boxes that had to line up BY POSITION;
  4. "Update sharing" flashed success while submitting nothing at all;
  5. a timestamp answered *when* but never *how soon*.

No network: the community route is always given typed names, so it never asks X
to resolve one.
"""
import dataclasses
import datetime as dt

import pytest

from aismm import config as config_module
from aismm.config import AuthSettings
from aismm.dashboard import app as app_module
from aismm.dashboard import sso
from aismm.dashboard.humanize import time_until
from aismm.models import (
    Account, ENV_VIDEO_ID, Instruction, PlatformName, ProviderConfig,
)
from aismm.workspaces import LOCAL_USER


@pytest.fixture()
def dash(store, monkeypatch, tmp_path):
    """The dashboard, with SSO configurable per test."""
    from aismm import assets as assets_module

    (tmp_path / "assets").mkdir(exist_ok=True)

    def make(auth=None):
        patched = dataclasses.replace(config_module.settings, auth=auth or AuthSettings(),
                                      data_dir=tmp_path)
        for module in (sso, app_module, config_module, assets_module):
            monkeypatch.setattr(module, "settings", patched)
        application = app_module.create_app()
        application.secret_key = "test"
        return application

    monkeypatch.setattr(app_module, "get_store", lambda: store)
    return make


def _account(store, platform, handle="acc", **meta):
    account = store.upsert_account(
        Account(platform=platform, handle=handle, external_id=handle), access_token="t")
    if meta:
        account.set_meta(meta)
        account = store.upsert_account(account)
    return account


def _signed_in(application, email):
    client = application.test_client()
    with client.session_transaction() as sess:
        sess[sso._SESSION_USER] = {"email": email, "name": email, "at": 0}
    return client


# --- 1. a field that cannot take effect is not shown ---------------------------------- #

def _all_platforms(store):
    """One account per platform the form has fields for, so those fields are on
    the page at all — the X card needs a community to offer, too."""
    _account(store, PlatformName.instagram, "ig")
    _account(store, PlatformName.youtube, "yt")
    _account(store, PlatformName.twitter, "x", community_ids=["123"],
             community_names={"123": "AI Builders"})
    return {a.platform.value: a for a in store.list_accounts()}


def test_a_new_instruction_hides_every_platform_field(dash, store):
    """Nothing is selected yet, so nothing platform-specific applies."""
    _all_platforms(store)
    page = dash().test_client().get("/instructions/new").get_data(as_text=True)
    # Present in the DOM (hiding is a display decision, and the control still
    # submits), but hidden — and the note says why.
    assert 'name="youtube_privacy"' in page
    assert page.count("data-platform-note") >= 1
    for block in _blocks_with(page, "data-platform="):
        assert "hidden" in block


def test_an_instagram_instruction_hides_the_youtube_and_x_fields(dash, store):
    accounts = _all_platforms(store)
    instruction = store.upsert_instruction(
        Instruction(name="Comic", account_ids_json=f'["{accounts["instagram"].id}"]'))
    page = dash().test_client().get(
        f"/instructions/{instruction.id}/edit").get_data(as_text=True)
    for block in _blocks_with(page, "data-platform="):
        assert "hidden" in block, block


def test_a_youtube_instruction_shows_the_visibility_picker_only(dash, store):
    accounts = _all_platforms(store)
    instruction = store.upsert_instruction(
        Instruction(name="Shorts", account_ids_json=f'["{accounts["youtube"].id}"]'))
    page = dash().test_client().get(
        f"/instructions/{instruction.id}/edit").get_data(as_text=True)
    youtube = _one_block(page, 'data-platform="youtube"')
    twitter = _one_block(page, 'data-platform="twitter"')
    assert "hidden" not in youtube
    assert "hidden" in twitter


def test_an_x_instruction_shows_the_community_card(dash, store):
    accounts = _all_platforms(store)
    instruction = store.upsert_instruction(
        Instruction(name="Timeline", account_ids_json=f'["{accounts["twitter"].id}"]'))
    page = dash().test_client().get(
        f"/instructions/{instruction.id}/edit").get_data(as_text=True)
    assert "hidden" not in _one_block(page, 'data-platform="twitter"')
    assert "hidden" in _one_block(page, 'data-platform="youtube"')


def test_a_setting_already_in_effect_is_never_hidden(dash, store):
    """The accounts changed after the choice was made. Hiding a field that HOLDS
    a value would take a decision off the screen while it is still applied — so a
    field with a value keeps itself visible and can be changed or cleared."""
    accounts = _all_platforms(store)
    instruction = store.upsert_instruction(
        Instruction(name="Was on YouTube",
                    account_ids_json=f'["{accounts["instagram"].id}"]',
                    youtube_privacy="private"))
    page = dash().test_client().get(
        f"/instructions/{instruction.id}/edit").get_data(as_text=True)
    youtube = _one_block(page, 'data-platform="youtube"')
    assert "data-platform-keep" in youtube
    assert "hidden" not in youtube


def test_the_account_checklist_carries_its_platform(dash, store):
    """What the page's script watches — without it the fields could only be
    right on the first paint."""
    _account(store, PlatformName.instagram, "ig")
    page = dash().test_client().get("/instructions/new").get_data(as_text=True)
    assert 'data-account-platform="instagram"' in page


def _blocks_with(page, marker):
    """Every opening tag containing ``marker``."""
    out = []
    for chunk in page.split("<")[1:]:
        tag = chunk.split(">")[0]
        if marker in tag:
            out.append(tag)
    assert out, f"no tag carries {marker}"
    return out


def _one_block(page, marker):
    blocks = _blocks_with(page, marker)
    assert len(blocks) == 1, f"{marker} appears {len(blocks)} times"
    return blocks[0]


# --- 2. state and sorting on the instruction list ------------------------------------- #

def test_every_instruction_column_is_sortable(dash, store):
    store.upsert_instruction(Instruction(name="A"))
    page = dash().test_client().get("/instructions").get_data(as_text=True)
    for key in ("name", "accounts", "schedule", "next_run", "publish_mode",
                "media", "enabled"):
        assert f"sort={key}" in page, key


def test_sorting_by_state_groups_the_enabled_ones_first(dash, store):
    store.upsert_instruction(Instruction(name="Zeta", enabled=True))
    store.upsert_instruction(Instruction(name="Alpha", enabled=False))
    page = dash().test_client().get("/instructions?sort=enabled").get_data(as_text=True)
    assert page.index("Zeta") < page.index("Alpha")
    flipped = dash().test_client().get(
        "/instructions?sort=enabled&dir=desc").get_data(as_text=True)
    assert flipped.index("Alpha") < flipped.index("Zeta")


def test_an_unknown_sort_key_is_ignored(dash, store):
    """A query parameter must not reach an arbitrary attribute."""
    store.upsert_instruction(Instruction(name="A"))
    assert dash().test_client().get(
        "/instructions?sort=secrets_enc").status_code == 200


def test_state_is_a_coloured_pill_not_flat_text(dash, store):
    store.upsert_instruction(Instruction(name="On", enabled=True))
    store.upsert_instruction(Instruction(name="Off", enabled=False))
    page = dash().test_client().get("/instructions").get_data(as_text=True)
    assert 'class="state state-on"' in page and 'class="state state-off"' in page
    css = (__import__("pathlib").Path(__file__).resolve().parents[1]
           / "aismm/dashboard/static/style.css").read_text()
    assert ".state-on" in css and ".state-off" in css


# --- 3. one value per field ----------------------------------------------------------- #

def test_x_communities_are_rows_of_id_and_name(dash, store):
    account = _account(store, PlatformName.twitter, "x",
                       community_ids=["123", "456"],
                       community_names={"123": "AI Builders", "456": "Rust"})
    page = dash().test_client().get("/accounts").get_data(as_text=True)
    # One row per saved community, plus a blank one to type the next into.
    assert page.count('name="community_row_id"') == 3
    assert page.count('name="community_row_name"') == 3
    assert "AI Builders" in page and "123" in page
    assert account.meta["community_ids"] == ["123", "456"]


def test_community_rows_are_saved_in_order_with_their_names(dash, store):
    account = _account(store, PlatformName.twitter, "x")
    client = dash().test_client()
    client.post(f"/accounts/{account.id}/community", data={
        "community_row_id": ["111", "222", ""],                # the blank row is
        "community_row_name": ["First", "Second", ""],         # not a community
    })
    saved = store.get_account(account.id)
    assert saved.meta["community_ids"] == ["111", "222"]
    assert saved.meta["community_names"] == {"111": "First", "222": "Second"}


def test_a_repeated_community_row_is_not_a_second_community(dash, store):
    account = _account(store, PlatformName.twitter, "x")
    dash().test_client().post(f"/accounts/{account.id}/community", data={
        "community_row_id": ["111", "111"],
        "community_row_name": ["First", "First again"],
    })
    assert store.get_account(account.id).meta["community_ids"] == ["111"]


def test_the_free_text_community_field_is_still_accepted(dash, store):
    """``parse_community_entries`` stays the one place that knows the documented
    ``ID = Name`` grammar, so a bookmarked POST keeps working."""
    account = _account(store, PlatformName.twitter, "x")
    dash().test_client().post(f"/accounts/{account.id}/community",
                              data={"community_id": "999 = Legacy"})
    saved = store.get_account(account.id)
    assert saved.meta["community_ids"] == ["999"]
    assert saved.meta["community_names"] == {"999": "Legacy"}


def test_clearing_every_community_row_means_the_home_timeline(dash, store):
    account = _account(store, PlatformName.twitter, "x", community_ids=["123"],
                       community_id="123")
    dash().test_client().post(f"/accounts/{account.id}/community",
                              data={"community_row_id": ""})
    assert "community_ids" not in store.get_account(account.id).meta


def test_outreach_targets_are_rows_with_the_kind_picked(dash, store):
    """The stored text carries the kind as a sigil (``#``/``r/``/``@``) — a
    grammar to learn before the first value can be typed."""
    instruction = store.upsert_instruction(Instruction(
        name="Reach", engagement_targets="prompt engineering, #AI, r/rust, @openai"))
    page = dash().test_client().get(
        f"/instructions/{instruction.id}/edit").get_data(as_text=True)
    assert page.count('name="target_kind"') == 5      # four saved, plus a blank row
    for value in ("prompt engineering", "AI", "rust", "openai"):
        assert f'value="{value}"' in page
    assert 'name="engagement_targets"' not in page    # no free-text box left


def _save(client, instruction, **extra):
    data = {"name": "Reach", "brief": "b", "schedule": "", "task_type": "outreach",
            "publish_mode": "dry_run", "media_pref": "auto"}
    data.update(extra)
    return client.post("/instructions", data=data, follow_redirects=True)


def test_target_rows_are_joined_back_into_the_stored_grammar(dash, store):
    _save(dash().test_client(), None,
          target_kind=["keyword", "hashtag", "subreddit", "account"],
          target_value=["prompt engineering", "AI", "rust", "openai"])
    saved = [i for i in store.list_instructions() if i.name == "Reach"][0]
    assert saved.engagement_targets == "prompt engineering, #AI, r/rust, @openai"


def test_a_sigil_typed_into_the_value_is_not_doubled(dash, store):
    """Picking "Subreddit" and typing "r/rust" is the obvious thing to do."""
    _save(dash().test_client(), None,
          target_kind=["subreddit", "hashtag", "account"],
          target_value=["/r/rust", "#AI", "@openai"])
    saved = [i for i in store.list_instructions() if i.name == "Reach"][0]
    assert saved.engagement_targets == "r/rust, #AI, @openai"


def test_a_blank_target_row_is_not_a_target(dash, store):
    _save(dash().test_client(), None,
          target_kind=["keyword", "keyword"], target_value=["ai", "  "])
    saved = [i for i in store.list_instructions() if i.name == "Reach"][0]
    assert saved.engagement_targets == "ai"


def test_a_separator_inside_a_target_cannot_split_it_in_half(dash, store):
    """A comma SEPARATES targets in the stored text, so one inside a value has
    to go somewhere — a space is closer to what was meant than two targets."""
    _save(dash().test_client(), None,
          target_kind=["keyword"], target_value=["agents, tools"])
    saved = [i for i in store.list_instructions() if i.name == "Reach"][0]
    assert saved.engagement_targets == "agents tools"


def test_the_free_text_targets_field_is_still_accepted(dash, store):
    _save(dash().test_client(), None, engagement_targets="#AI, r/rust")
    saved = [i for i in store.list_instructions() if i.name == "Reach"][0]
    assert saved.engagement_targets == "#AI, r/rust"


def test_the_sora_pool_is_one_row_per_resource(dash, store):
    cfg = ProviderConfig(kind="video", name="Pool", created_by=LOCAL_USER)
    cfg.set_config({"endpoints_csv": "https://a.openai.azure.com, https://b.openai.azure.com",
                    "models_csv": "sora-2, sora-2"})
    store.upsert_provider_config(cfg, secrets={"keys_csv": "k1, k2"})
    page = dash().test_client().get("/settings").get_data(as_text=True)
    # Two saved resources plus a blank row to type the next into, and one more
    # blank row in the "add a connection" form below — no CSV boxes left.
    assert page.count('name="pool_endpoint"') == 4
    assert 'name="endpoints_csv"' not in page and 'name="keys_csv"' not in page
    assert "https://b.openai.azure.com" in page
    assert "k1" not in page                     # keys are never echoed back


def test_saving_the_pool_rebuilds_the_aligned_csvs(dash, store):
    client = dash().test_client()
    client.post("/settings/provider/video", data={
        "name": "Pool", "enabled": "on", "api_version": "preview", "max_attempts": "0",
        "pool_endpoint": ["https://a.openai.azure.com", "https://b.openai.azure.com", ""],
        "pool_model": ["sora-2", "", ""],
        "pool_key": ["key-a", "key-b", ""],
    })
    cfg = [c for c in store.list_provider_configs(kind="video") if c.id != ENV_VIDEO_ID][0]
    assert cfg.config["endpoints_csv"] == (
        "https://a.openai.azure.com, https://b.openai.azure.com")
    assert cfg.config["models_csv"] == "sora-2, sora-2"     # the blank model defaults
    pool = store.resolve_sora_settings(cfg.id).pool()
    assert [(r["endpoint"], r["key"]) for r in pool] == [
        ("https://a.openai.azure.com", "key-a"), ("https://b.openai.azure.com", "key-b")]


def test_a_pool_saved_without_keys_keeps_the_stored_ones(dash, store):
    cfg = ProviderConfig(kind="video", name="Pool", created_by=LOCAL_USER)
    cfg.set_config({"endpoints_csv": "https://a.openai.azure.com",
                    "models_csv": "sora-2"})
    cfg = store.upsert_provider_config(cfg, secrets={"keys_csv": "kept"})
    dash().test_client().post("/settings/provider/video", data={
        "id": cfg.id, "name": "Renamed", "enabled": "on",
        "api_version": "preview", "max_attempts": "0",
        "pool_endpoint": "https://a.openai.azure.com", "pool_model": "sora-2",
        "pool_key": "",
    })
    assert store.get_provider_config(cfg.id).name == "Renamed"
    assert store.resolve_sora_settings(cfg.id).pool()[0]["key"] == "kept"


def test_a_changed_pool_cannot_keep_keys_that_no_longer_line_up(dash, store):
    """The stored keys are aligned to the stored endpoints by position, so adding
    a resource without its key would put the first key on the second resource."""
    cfg = ProviderConfig(kind="video", name="Pool", created_by=LOCAL_USER)
    cfg.set_config({"endpoints_csv": "https://a.openai.azure.com", "models_csv": "sora-2"})
    cfg = store.upsert_provider_config(cfg, secrets={"keys_csv": "kept"})
    resp = dash().test_client().post("/settings/provider/video", data={
        "id": cfg.id, "name": "Pool", "enabled": "on",
        "api_version": "preview", "max_attempts": "0",
        "pool_endpoint": "https://new.openai.azure.com", "pool_model": "sora-2",
        "pool_key": "",
    }, follow_redirects=True)
    assert "no longer line up" in resp.get_data(as_text=True)
    saved = store.get_provider_config(cfg.id)
    assert saved.config["endpoints_csv"] == "https://a.openai.azure.com"
    assert store.resolve_sora_settings(cfg.id).pool()[0]["key"] == "kept"


def test_some_keys_typed_and_some_blank_is_refused(dash, store):
    resp = dash().test_client().post("/settings/provider/video", data={
        "name": "Pool", "enabled": "on", "api_version": "preview", "max_attempts": "0",
        "pool_endpoint": ["https://a.openai.azure.com", "https://b.openai.azure.com"],
        "pool_key": ["key-a", ""],
    }, follow_redirects=True)
    assert "own API key" in resp.get_data(as_text=True)
    assert [c for c in store.list_provider_configs(kind="video")
            if c.id != ENV_VIDEO_ID] == []


def test_a_pool_with_no_resources_is_refused(dash, store):
    resp = dash().test_client().post("/settings/provider/video", data={
        "name": "Empty", "enabled": "on", "pool_endpoint": "", "pool_key": "",
    }, follow_redirects=True)
    assert "at least one Sora resource" in resp.get_data(as_text=True)


# --- 4. sharing that actually shares --------------------------------------------------- #

def test_with_sso_off_there_is_no_sharing_form_to_press(dash, store):
    """The button used to flash "Sharing updated." over a form with nothing in
    it: no workspace checkbox on an .env config, and a people picker built from
    users this deployment has never had."""
    page = dash().test_client().get("/settings").get_data(as_text=True)
    assert 'name="share_add"' not in page
    assert "Sharing needs sign-in" in page


def _sso():
    return AuthSettings(issuer="https://issuer.example", client_id="c",
                        client_secret="s", allowed_emails=["me@x.com", "you@x.com"],
                        owner_emails=["me@x.com"])


def test_the_owner_can_share_with_anyone_by_typing_an_address(dash, store):
    cfg = ProviderConfig(kind="image", name="Images", created_by="me@x.com")
    cfg.set_config({"endpoint": "https://e", "model": "gpt-image-2"})
    cfg = store.upsert_provider_config(cfg, secrets={"api_key": "k"})
    client = _signed_in(dash(_sso()), "me@x.com")
    resp = client.post(f"/settings/provider/{cfg.id}/share",
                       data={"share_add": "You@X.com"}, follow_redirects=True)
    assert store.get_provider_config(cfg.id).shared_with == ["you@x.com"]
    # The flash says what actually happened, not "Sharing updated."
    assert "shared with 1 person" in resp.get_data(as_text=True)


def test_removing_the_row_is_what_stops_the_sharing(dash, store):
    cfg = ProviderConfig(kind="image", name="Images", created_by="me@x.com")
    cfg.set_config({"endpoint": "https://e", "model": "gpt-image-2"})
    cfg.set_shared_with(["you@x.com", "them@x.com"])
    cfg = store.upsert_provider_config(cfg, secrets={"api_key": "k"})
    client = _signed_in(dash(_sso()), "me@x.com")
    # The form posts back the rows that were NOT removed.
    client.post(f"/settings/provider/{cfg.id}/share", data={"shared_with": "you@x.com"})
    assert store.get_provider_config(cfg.id).shared_with == ["you@x.com"]
    resp = client.post(f"/settings/provider/{cfg.id}/share", data={},
                       follow_redirects=True)
    assert store.get_provider_config(cfg.id).shared_with == []
    assert "private to you" in resp.get_data(as_text=True)


def test_something_that_is_not_an_address_is_reported_not_dropped(dash, store):
    cfg = ProviderConfig(kind="image", name="Images", created_by="me@x.com")
    cfg.set_config({"endpoint": "https://e", "model": "gpt-image-2"})
    cfg = store.upsert_provider_config(cfg, secrets={"api_key": "k"})
    client = _signed_in(dash(_sso()), "me@x.com")
    resp = client.post(f"/settings/provider/{cfg.id}/share",
                       data={"share_add": "my colleague"}, follow_redirects=True)
    assert "is not an email address" in resp.get_data(as_text=True)
    assert store.get_provider_config(cfg.id).shared_with == []


def test_the_current_shares_are_on_the_page_with_a_way_to_remove_them(dash, store):
    cfg = ProviderConfig(kind="image", name="Images", created_by="me@x.com")
    cfg.set_config({"endpoint": "https://e", "model": "gpt-image-2"})
    cfg.set_shared_with(["you@x.com"])
    store.upsert_provider_config(cfg, secrets={"api_key": "k"})
    page = _signed_in(dash(_sso()), "me@x.com").get("/settings").get_data(as_text=True)
    assert 'name="shared_with" value="you@x.com"' in page
    assert "Stop sharing with you@x.com" in page


# --- 5. how soon, not just when ------------------------------------------------------- #

@pytest.mark.parametrize("seconds,expected", [
    (180, "in 3 minutes"),
    (-7200, "2 hours ago"),
    (10, "now"),
    (86400 * 3, "in 3 days"),
    (5400, "in 1h 30m"),
])
def test_a_time_is_also_given_as_a_distance_from_now(seconds, expected):
    now = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.timezone.utc)
    assert time_until(now + dt.timedelta(seconds=seconds), now=now) == expected


def test_an_unreadable_time_contributes_nothing(dash):
    """A broken relative line beside a correct timestamp is worse than none."""
    assert time_until(None) == ""
    assert time_until("not a date") == ""
    assert time_until(object()) == ""


def test_a_naive_timestamp_is_read_as_utc():
    """SQLite hands back naive datetimes; Azure Table hands back ISO strings."""
    now = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.timezone.utc)
    assert time_until(dt.datetime(2026, 9, 3, 12, 5), now=now) == "in 5 minutes"
    assert time_until("2026-09-03T12:05:00", now=now) == "in 5 minutes"


def test_the_runs_list_says_how_long_ago(dash, store, ):
    from aismm.models import Run, RunStatus

    instruction = store.upsert_instruction(Instruction(name="A"))
    account = _account(store, PlatformName.twitter, "x")
    run = Run(instruction_id=instruction.id, account_id=account.id,
              status=RunStatus.published)
    run.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    store.add_run(run)
    page = dash().test_client().get("/runs").get_data(as_text=True)
    assert "2 hours ago" in page
