"""YouTube visibility: private / unlisted / public, per instruction.

It used to be one deployment-wide `os.getenv("YOUTUBE_PRIVACY", "private")`, so
every upload from every instruction landed the same way and there was no route to
publishing publicly from the dashboard at all. One channel commonly runs a public
series and a staging instruction that should stay unlisted.

The sharp edge is YouTube's: an API project that has not passed the compliance
audit has EVERY upload locked to private whatever was requested, and the lock
cannot be appealed — the video has to be re-uploaded through an audited client.
Reporting a clean "published" over a silently private video is the worst outcome,
so the upload compares what it asked for with what came back.
"""
import asyncio
import dataclasses

import httpx
import pytest

from aismm import config as config_module
from aismm.config import AuthSettings, YOUTUBE_PRIVACY_CHOICES
from aismm.models import Account, Instruction, PlatformName
from aismm.platforms.youtube import YouTube, resolve_privacy


def _instruction(**kw):
    return Instruction(name="Daily short", brief="b", **kw)


# --- choosing the visibility -------------------------------------------------------------- #

def test_an_instruction_can_publish_publicly():
    assert resolve_privacy(_instruction(youtube_privacy="public")) == "public"


@pytest.mark.parametrize("choice", YOUTUBE_PRIVACY_CHOICES)
def test_every_choice_is_honoured(choice):
    assert resolve_privacy(_instruction(youtube_privacy=choice)) == choice


def test_no_choice_falls_back_to_the_deployment_default():
    assert resolve_privacy(_instruction()) == config_module.settings.youtube_privacy


def test_no_instruction_at_all_still_resolves():
    assert resolve_privacy(None) in YOUTUBE_PRIVACY_CHOICES


def test_a_nonsense_value_is_not_sent_to_youtube():
    """YouTube rejects an unknown privacyStatus with a generic 400."""
    assert resolve_privacy(_instruction(youtube_privacy="everyone")) == "private"


def test_the_default_is_read_from_settings_not_the_environment(monkeypatch):
    """Config is a frozen singleton built at import; a stray os.getenv is
    invisible to the tests that pin the environment."""
    from pathlib import Path as _P

    # The module does not import `os` at all — the invariant, rather than a
    # grep for "os.getenv" that a docstring mentioning it would trip.
    source = _P("aismm/platforms/youtube.py").read_text()
    assert "\nimport os\n" not in source

    from aismm.platforms import youtube

    patched = dataclasses.replace(config_module.settings, youtube_privacy="unlisted")
    monkeypatch.setattr(youtube, "settings", patched)
    assert resolve_privacy(_instruction()) == "unlisted"


# --- the upload ---------------------------------------------------------------------------- #

def _upload(monkeypatch, instruction, *, returns_privacy=None):
    sent = {}

    def handler(request):
        if request.method == "POST":
            import json as _json
            sent["metadata"] = _json.loads(request.content.decode())
            return httpx.Response(200, headers={"Location": "https://upload/session"})
        body = {"id": "vid123"}
        if returns_privacy is not None:
            body["status"] = {"privacyStatus": returns_privacy}
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def client(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr("aismm.platforms.youtube.read_bytes", lambda p: b"video-bytes")
    account = Account(platform=PlatformName.youtube, handle="chan", external_id="UC1")
    result = asyncio.run(YouTube(None).publish(
        access_token="t", account=account, caption="Title\nDescription",
        asset_path="/a/clip.mp4", media_kind="video", instruction=instruction))
    return sent, result


def test_public_is_what_reaches_the_api(monkeypatch):
    sent, _ = _upload(monkeypatch, _instruction(youtube_privacy="public"))
    assert sent["metadata"]["status"]["privacyStatus"] == "public"


def test_the_video_url_is_still_returned(monkeypatch):
    _sent, result = _upload(monkeypatch, _instruction(youtube_privacy="public"))
    assert result.external_id == "vid123"
    assert result.url == "https://youtu.be/vid123"


def test_a_silent_downgrade_to_private_is_reported(monkeypatch):
    """The unaudited-project lock. Without this the run says "published" and the
    video is private."""
    _sent, result = _upload(monkeypatch, _instruction(youtube_privacy="public"),
                            returns_privacy="private")
    notice = result.raw["notice"]
    assert "PRIVATE, not public" in notice
    assert "compliance audit" in notice
    assert "cannot be appealed" in notice


def test_no_notice_when_youtube_honoured_the_request(monkeypatch):
    _sent, result = _upload(monkeypatch, _instruction(youtube_privacy="public"),
                            returns_privacy="public")
    assert "notice" not in result.raw


def test_no_notice_when_youtube_says_nothing(monkeypatch):
    """Absence of `status` in the response is not evidence of a downgrade."""
    _sent, result = _upload(monkeypatch, _instruction(youtube_privacy="public"))
    assert "notice" not in result.raw


def test_the_notice_reaches_the_run_log(store, monkeypatch):
    """It has to land where the operator looks, not only in the platform's return."""
    import inspect

    from aismm.tools import publish_tool

    source = inspect.getsource(publish_tool)
    assert 'notice = str((result.raw or {}).get("notice")' in source
    assert 'f"\\nNOTE: {notice}" if notice else ""' in source


# --- storage and the form -------------------------------------------------------------------- #

def test_it_survives_the_azure_whitelist():
    from aismm.store.azure_store import AzureStore

    instruction = _instruction(youtube_privacy="public")
    entity = AzureStore._instruction_to_entity(instruction)
    entity["RowKey"] = instruction.id
    assert AzureStore._instruction_from_entity(entity).youtube_privacy == "public"


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


def test_the_form_offers_it_when_a_youtube_account_is_connected(dash, store):
    store.upsert_account(Account(platform=PlatformName.youtube, handle="chan",
                                 external_id="UC1"), access_token="t")
    page = dash.test_client().get("/instructions/new").get_data(as_text=True)
    assert 'name="youtube_privacy"' in page
    assert ">Public<" in page or "Public" in page
    assert "compliance audit" in page          # the lock is stated where it is chosen


def test_the_form_hides_it_without_one(dash, store):
    store.upsert_account(Account(platform=PlatformName.instagram, handle="ig",
                                 external_id="1"), access_token="t")
    page = dash.test_client().get("/instructions/new").get_data(as_text=True)
    assert 'name="youtube_privacy"' not in page


def test_the_form_saves_public(dash, store):
    store.upsert_account(Account(platform=PlatformName.youtube, handle="chan",
                                 external_id="UC1"), access_token="t")
    dash.test_client().post("/instructions", data={
        "name": "Daily short", "brief": "b", "schedule": "03:00 mon",
        "publish_mode": "dry_run", "task_type": "publish", "media_pref": "auto",
        "youtube_privacy": "public"}, follow_redirects=True)
    assert store.list_instructions()[0].youtube_privacy == "public"


def test_a_forged_value_is_rejected_at_the_form(dash, store):
    store.upsert_account(Account(platform=PlatformName.youtube, handle="chan",
                                 external_id="UC1"), access_token="t")
    dash.test_client().post("/instructions", data={
        "name": "Daily short", "brief": "b", "schedule": "03:00 mon",
        "publish_mode": "dry_run", "task_type": "publish", "media_pref": "auto",
        "youtube_privacy": "everyone"}, follow_redirects=True)
    assert store.list_instructions()[0].youtube_privacy == ""
