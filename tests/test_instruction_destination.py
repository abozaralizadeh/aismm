"""Per-instruction X destination: which community, and who else sees it.

The community and the "Also share with followers" switch were account-wide, but
one account often runs several instructions that should not all post to the same
place — a niche community for one, the home timeline for another. So an
instruction may PIN a destination, and the account setting becomes the default.

Communities are chosen by NAME. A 19-digit id is not something anyone
recognises: names come from X's own API where it will answer, and from a label
the operator types where it will not.
"""
import dataclasses

import pytest

from aismm import config as config_module
from aismm.config import AuthSettings
from aismm.models import Account, Instruction, PlatformName
from aismm.platforms.twitter import (
    HOME_TIMELINE, community_label, next_community, parse_community_entries,
    shares_with_followers,
)


def _account(**meta):
    account = Account(platform=PlatformName.twitter, handle="abo0zar", external_id="9")
    account.set_meta(meta)
    return account


def _instruction(**kw):
    return Instruction(name="i", brief="b", schedule="03:00 mon", **kw)


# --- choosing the destination ------------------------------------------------------------ #

def test_an_instruction_pins_its_own_community():
    account = _account(community_ids=["111", "222"], community_cursor=0)
    assert next_community(account, _instruction(twitter_community_id="222")) == "222"


def test_an_instruction_can_pin_the_home_timeline():
    """Distinct from "no preference": the account HAS communities, and this
    instruction deliberately posts past them."""
    account = _account(community_ids=["111", "222"])
    assert next_community(account, _instruction(twitter_community_id=HOME_TIMELINE)) == ""


def test_no_pin_inherits_the_account_rotation():
    account = _account(community_ids=["111", "222"], community_cursor=1)
    assert next_community(account, _instruction()) == "222"


def test_no_instruction_at_all_still_works():
    """perform_publish is not the only caller."""
    account = _account(community_ids=["111"])
    assert next_community(account) == "111"


def test_a_pinned_instruction_does_not_advance_the_rotation(store):
    """It never used the rotation. Advancing it would walk the cursor past the
    communities the ROTATING instructions feed."""
    from aismm.platforms.registry import get_platform

    account = store.upsert_account(_account(community_ids=["111", "222", "333"],
                                            community_cursor=0), access_token="t")
    get_platform(PlatformName.twitter).after_publish(account=account, store=store, result=None,
                            instruction=_instruction(twitter_community_id="333"))
    assert (store.get_account(account.id).meta or {}).get("community_cursor", 0) == 0


def test_an_inheriting_instruction_does_advance_it(store):
    from aismm.platforms.registry import get_platform

    account = store.upsert_account(_account(community_ids=["111", "222"],
                                            community_cursor=0), access_token="t")
    get_platform(PlatformName.twitter).after_publish(account=account, store=store, result=None,
                            instruction=_instruction())
    assert store.get_account(account.id).meta["community_cursor"] == 1


# --- who else sees it --------------------------------------------------------------------- #

@pytest.mark.parametrize("override,account_setting,expected", [
    ("", True, True),            # inherit
    ("", False, False),
    ("yes", False, True),        # override on
    ("no", True, False),         # override off
])
def test_share_with_followers_is_tri_state(override, account_setting, expected):
    account = _account(community_ids=["111"], share_with_followers=account_setting)
    instruction = _instruction(twitter_share_with_followers=override)
    assert shares_with_followers(account, instruction) is expected


# --- names, not ids ----------------------------------------------------------------------- #

def test_a_community_is_labelled_by_name_when_we_know_it():
    account = _account(community_ids=["111"], community_names={"111": "AI Builders"})
    assert community_label(account, "111") == "AI Builders"


def test_an_unknown_community_falls_back_to_its_id():
    """Never blank: an id the operator can still recognise beats nothing."""
    assert community_label(_account(community_ids=["111"]), "111") == "111"


def test_the_operator_can_label_an_id_by_hand():
    assert parse_community_entries("111 = AI Builders") == [("111", "AI Builders")]


def test_a_labelled_entry_owns_its_whole_line():
    """Names contain commas — "AI, Robotics & Agents" must not become two ids."""
    entries = parse_community_entries("111 = AI, Robotics & Agents\n222 = Indie Devs")
    assert entries == [("111", "AI, Robotics & Agents"), ("222", "Indie Devs")]


def test_bare_ids_still_parse_the_old_way():
    assert parse_community_entries("111, 222 333") == [("111", ""), ("222", ""), ("333", "")]


def test_duplicates_are_dropped_keeping_the_first():
    assert parse_community_entries("111 = A\n111 = B") == [("111", "A")]


# --- the form ----------------------------------------------------------------------------- #

@pytest.fixture()
def dash(monkeypatch, store, tmp_path):
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


@pytest.fixture()
def x_account(store):
    return store.upsert_account(
        _account(community_ids=["111", "222"],
                 community_names={"111": "AI Builders", "222": "Indie Hackers"}),
        access_token="t")


def test_the_form_offers_the_communities_by_name(dash, x_account):
    page = dash.test_client().get("/instructions/new").get_data(as_text=True)
    assert "AI Builders" in page and "Indie Hackers" in page
    assert "The home timeline" in page
    assert "Use the account's setting" in page


def test_the_form_is_absent_without_any_x_community(dash, store):
    """No communities, no choice to make — the section would be noise."""
    store.upsert_account(_account(), access_token="t")
    page = dash.test_client().get("/instructions/new").get_data(as_text=True)
    assert 'name="twitter_community_id"' not in page


def _save(dash, **extra):
    form = {"name": "i", "brief": "b", "schedule": "03:00 mon",
            "publish_mode": "dry_run", "task_type": "publish", "media_pref": "auto",
            **extra}
    return dash.test_client().post("/instructions", data=form, follow_redirects=True)


def test_a_choice_is_saved(dash, store, x_account):
    _save(dash, twitter_community_id="222", twitter_share_with_followers="no")
    instruction = store.list_instructions()[0]
    assert instruction.twitter_community_id == "222"
    assert instruction.twitter_share_with_followers == "no"


def test_the_home_timeline_choice_is_saved(dash, store, x_account):
    _save(dash, twitter_community_id="none")
    assert store.list_instructions()[0].twitter_community_id == "none"


def test_a_community_the_account_no_longer_has_falls_back_to_inheriting(dash, store, x_account):
    """A stale pick must not post somewhere the operator can no longer see."""
    _save(dash, twitter_community_id="999999")
    assert store.list_instructions()[0].twitter_community_id == ""


def test_a_nonsense_share_value_is_ignored(dash, store, x_account):
    _save(dash, twitter_share_with_followers="maybe")
    assert store.list_instructions()[0].twitter_share_with_followers == ""


def test_the_choice_survives_a_round_trip_through_azure(x_account):
    """Azure's entity mapping is an explicit whitelist — a new column silently
    vanishes there unless it is added to BOTH halves."""
    from aismm.store.azure_store import AzureStore

    instruction = _instruction(twitter_community_id="222",
                               twitter_share_with_followers="yes")
    entity = AzureStore._instruction_to_entity(instruction)
    assert entity["twitter_community_id"] == "222"
    entity["RowKey"] = instruction.id
    back = AzureStore._instruction_from_entity(entity)
    assert back.twitter_community_id == "222"
    assert back.twitter_share_with_followers == "yes"
