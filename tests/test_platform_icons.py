"""Platform brand marks beside the account count.

"3 accounts" does not say WHERE an instruction posts, which is the thing you
want when scanning the list. The marks say it in less room than the words.

They come from Simple Icons (CC0) and live as SVG files; this module reads the
path out of them so the file stays the single definition.
"""
import dataclasses

import pytest

from aismm.dashboard import platform_icons
from aismm.models import PlatformName


def test_every_platform_has_a_mark():
    """A platform added without an icon would show a bare number forever."""
    assert set(platform_icons.known()) == {p.value for p in PlatformName}


def test_a_mark_is_inlined_not_linked():
    """A table row must not fire four extra requests, and an <img> cannot be
    recoloured for the dark theme."""
    svg = str(platform_icons.icon("instagram"))
    assert svg.startswith("<svg") and "<path d=" in svg
    assert "<img" not in svg


def test_monochrome_marks_inherit_the_text_colour():
    """X and TikTok publish monochrome marks — black on the dark dashboard would
    be invisible."""
    for name in ("twitter", "tiktok"):
        assert 'fill="currentColor"' in str(platform_icons.icon(name))


def test_coloured_marks_keep_their_brand_colour():
    assert 'fill="#E4405F"' in str(platform_icons.icon("instagram"))
    assert 'fill="#FF0000"' in str(platform_icons.icon("youtube"))


def test_an_unknown_platform_renders_nothing():
    assert str(platform_icons.icon("myspace")) == ""
    assert str(platform_icons.icon("")) == ""
    assert str(platform_icons.icon(None)) == ""


def test_the_platform_is_named_for_a_screen_reader():
    """The icon is decoration on top of information, never the only copy of it."""
    svg = str(platform_icons.icon("youtube"))
    assert 'aria-label="youtube"' in svg and "<title>youtube</title>" in svg


def test_a_missing_icon_file_does_not_break_the_page(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_icons, "ICON_DIR", tmp_path)
    monkeypatch.setattr(platform_icons, "_paths", {})
    platform_icons._load()
    assert str(platform_icons.icon("instagram")) == ""


def test_the_marks_are_the_real_thing_and_their_licence_is_recorded():
    """CC0, and the file says where to re-download from — otherwise the next
    person hand-edits a trademark."""
    notice = (platform_icons.ICON_DIR / "NOTICE.txt").read_text()
    assert "CC0" in notice and "simpleicons.org" in notice


# --- in the instruction list ------------------------------------------------------------ #

@pytest.fixture()
def dashboard(monkeypatch, store, tmp_path):
    from aismm import config as config_module
    from aismm.config import AuthSettings
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


def _instruction(store, accounts):
    from aismm.models import Instruction

    instruction = Instruction(name="Daily reel", brief="b", schedule="03:00 mon")
    instruction.set_account_ids([a.id for a in accounts])
    store.upsert_instruction(instruction)
    return instruction


def _account(store, platform, handle):
    from aismm.models import Account

    account = Account(platform=platform, handle=handle, display_name=handle)
    store.upsert_account(account)
    return account


def test_the_list_shows_a_mark_per_platform(dashboard, store):
    ig = _account(store, PlatformName.instagram, "ig")
    x = _account(store, PlatformName.twitter, "x")
    _instruction(store, [ig, x])

    body = dashboard.test_client().get("/instructions").get_data(as_text=True)
    assert 'aria-label="instagram"' in body
    assert 'aria-label="X"' in body
    assert 'aria-label="youtube"' not in body       # not one of its accounts


def test_two_accounts_on_one_platform_show_one_mark(dashboard, store):
    """The marks answer 'where', and the count already answers 'how many'."""
    first = _account(store, PlatformName.instagram, "one")
    second = _account(store, PlatformName.instagram, "two")
    _instruction(store, [first, second])

    body = dashboard.test_client().get("/instructions").get_data(as_text=True)
    assert body.count('aria-label="instagram"') == 1


def test_a_disconnected_account_simply_drops_out(dashboard, store):
    """An instruction can outlive an account; a stale id must not 500 the page."""
    ig = _account(store, PlatformName.instagram, "ig")
    instruction = _instruction(store, [ig])
    instruction.set_account_ids([ig.id, "deleted-account-id"])
    store.upsert_instruction(instruction)

    response = dashboard.test_client().get("/instructions")
    assert response.status_code == 200
    assert 'aria-label="instagram"' in response.get_data(as_text=True)


def test_an_instruction_with_no_accounts_shows_no_marks(dashboard, store):
    _instruction(store, [])
    body = dashboard.test_client().get("/instructions").get_data(as_text=True)
    assert "platform-marks" not in body
