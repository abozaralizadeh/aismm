"""User-managed IMAGE and VIDEO (Sora) connections — ``ProviderConfig``.

The image/video parallel of ``test_llm_connections``: the same sharing model
(own / workspace-shared / owner-people-shared, plus an ``.env`` sentinel) applied
to the two generation providers. What must hold, pinned here without a network:

* a config round-trips its non-secret fields in BOTH backends, and the secret(s)
  round-trip ONLY through the decrypt path (``resolve_image_settings`` /
  ``resolve_sora_settings``) — ``list_provider_configs`` never leaks plaintext;
* the env sentinels resolve to ``settings.image`` / ``settings.sora``;
* a video config carries a POOL (comma-separated endpoints/keys/models);
* the shared ACL (``can_select`` / ``visible_provider_configs``) works on a
  ``ProviderConfig`` unchanged;
* image/video are OPTIONAL — an inaccessible/disabled config resolves to ``None``
  (tool disabled), never a run failure;
* the runtime threads a per-run image ``ImageSettings`` and Sora pool without
  mutating any global (image client cache; ``sora_config`` ContextVar).
"""
import pytest

from aismm import config as config_module
from aismm.config import ImageSettings, SoraSettings
from aismm.llm_access import (
    can_select, env_provider_config, is_owned, visible_provider_configs,
)
from aismm.models import ENV_IMAGE_ID, ENV_VIDEO_ID, Instruction, ProviderConfig


def _both_backends():
    from aismm.store.azure_store import AzureStore
    from aismm.store.local_store import LocalStore
    from tests.test_azure_store import FakeTableClient

    return [
        ("local", LocalStore(db_url="sqlite:///:memory:")),
        ("azure", AzureStore(table_client=FakeTableClient())),
    ]


_ID = pytest.mark.parametrize(
    "label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")

_KEY = "img-secret-key-abc123"
_KEYS = "sora-key-1,sora-key-2"


# --- round-trip + decrypt, both backends -------------------------------------------- #

@_ID
def test_image_config_round_trips_and_decrypts(label, store):
    store.init()
    cfg = ProviderConfig(kind="image", workspace_id="w1", created_by="me@x.com",
                         name="My Images")
    cfg.set_config({"endpoint": "https://img.openai.azure.com",
                    "api_version": "2025-04-01-preview", "model": "gpt-image-2"})
    cfg = store.upsert_provider_config(cfg, secrets={"api_key": _KEY})

    got = store.get_provider_config(cfg.id)
    assert got is not None and got.kind == "image" and got.name == "My Images"
    assert got.config["model"] == "gpt-image-2"
    assert got.config["endpoint"] == "https://img.openai.azure.com"

    img = store.resolve_image_settings(cfg.id)
    assert isinstance(img, ImageSettings)
    assert img.api_key == _KEY and img.model == "gpt-image-2"
    assert img.endpoint == "https://img.openai.azure.com" and img.enabled


@_ID
def test_video_config_round_trips_as_a_pool(label, store):
    """A video connection may hold several endpoints/keys/models — the pool the
    user asked for, aligned by index exactly like ``SoraSettings.pool()``."""
    store.init()
    cfg = ProviderConfig(kind="video", created_by="me@x.com", name="My Sora")
    cfg.set_config({"endpoints_csv": "https://a.openai.azure.com,https://b.openai.azure.com",
                    "models_csv": "sora-2,sora-2", "api_version": "preview",
                    "max_attempts": "2"})
    cfg = store.upsert_provider_config(cfg, secrets={"keys_csv": _KEYS})

    sora = store.resolve_sora_settings(cfg.id)
    assert isinstance(sora, SoraSettings)
    assert sora.max_attempts == 2 and sora.api_version == "preview"
    pool = sora.pool()
    assert [r["endpoint"] for r in pool] == [
        "https://a.openai.azure.com", "https://b.openai.azure.com"]
    assert [r["key"] for r in pool] == ["sora-key-1", "sora-key-2"]
    assert sora.enabled


@_ID
def test_list_provider_configs_never_leaks_a_decrypted_secret(label, store):
    store.init()
    cfg = ProviderConfig(kind="image", created_by="me@x.com", name="A")
    cfg.set_config({"endpoint": "https://e", "model": "gpt-image-2"})
    store.upsert_provider_config(cfg, secrets={"api_key": _KEY})
    for got in store.list_provider_configs():
        for value in vars(got).values():
            assert value != _KEY
        assert _KEY not in (got.secrets_enc or "")


@_ID
def test_upsert_preserves_secret_when_not_provided(label, store):
    store.init()
    cfg = ProviderConfig(kind="image", created_by="me@x.com", name="A")
    cfg.set_config({"endpoint": "https://e", "model": "gpt-image-2"})
    cfg = store.upsert_provider_config(cfg, secrets={"api_key": _KEY})
    cfg.name = "Renamed"
    store.upsert_provider_config(cfg)  # no secrets kwarg → preserve
    assert store.resolve_image_settings(cfg.id).api_key == _KEY
    assert store.get_provider_config(cfg.id).name == "Renamed"


@_ID
def test_list_provider_configs_filters_by_kind(label, store):
    # The store is shared across this module's parametrized cases, so scope the
    # assertions to configs THIS test created rather than the whole table.
    store.init()
    who = "filt@x.com"
    store.upsert_provider_config(ProviderConfig(kind="image", name="i", created_by=who),
                                 secrets={"api_key": "k"})
    store.upsert_provider_config(ProviderConfig(kind="video", name="v", created_by=who),
                                 secrets={"keys_csv": "k"})
    imgs = [c.name for c in store.list_provider_configs(kind="image") if c.created_by == who]
    vids = [c.name for c in store.list_provider_configs(kind="video") if c.created_by == who]
    assert imgs == ["i"] and vids == ["v"]


# --- env sentinels + optional (None) behaviour -------------------------------------- #

@_ID
def test_env_sentinels_resolve_to_deployment_settings(label, store):
    store.init()
    assert store.resolve_image_settings(ENV_IMAGE_ID) == config_module.settings.image
    assert store.resolve_sora_settings(ENV_VIDEO_ID) == config_module.settings.sora


@_ID
def test_missing_or_disabled_resolves_to_none(label, store):
    store.init()
    assert store.resolve_image_settings("nope") is None
    assert store.resolve_sora_settings("nope") is None
    cfg = store.upsert_provider_config(
        ProviderConfig(kind="image", name="off", enabled=False),
        secrets={"api_key": "k"})
    assert store.resolve_image_settings(cfg.id) is None


# --- the shared ACL works on a ProviderConfig unchanged ----------------------------- #

def test_can_select_matrix_for_provider_configs():
    mine = ProviderConfig(kind="image", created_by="me@x.com", workspace_id="w1")
    theirs = ProviderConfig(kind="image", created_by="you@x.com", workspace_id="w2")
    ws_shared = ProviderConfig(kind="image", created_by="you@x.com", workspace_id="w1",
                               shared_with_workspace=True)
    people = ProviderConfig(kind="image", created_by="you@x.com", workspace_id="w2")
    people.set_shared_with(["me@x.com"])

    # Own: always.
    assert can_select(mine, "me@x.com", {"w1"}, is_owner=False)
    # A stranger's private config: never.
    assert not can_select(theirs, "me@x.com", {"w1"}, is_owner=False)
    # Workspace-shared: a member of that workspace may select.
    assert can_select(ws_shared, "me@x.com", {"w1"}, is_owner=False)
    assert not can_select(ws_shared, "me@x.com", {"w9"}, is_owner=False)
    # People-shared: the named person may select.
    assert can_select(people, "me@x.com", {"w1"}, is_owner=False)


def test_env_provider_config_is_a_sentinel():
    img = env_provider_config("image")
    vid = env_provider_config("video")
    assert img.id == ENV_IMAGE_ID and img.is_env and img.kind == "image"
    assert vid.id == ENV_VIDEO_ID and vid.is_env and vid.kind == "video"


@_ID
def test_visible_provider_configs_is_own_first(label, store):
    # A test-unique identity + workspace so residual configs from other cases in
    # this shared store don't enter the visible set.
    store.init()
    me, ws = "viz@x.com", "wviz"
    store.upsert_provider_config(
        ProviderConfig(kind="image", created_by="other@x.com", workspace_id=ws,
                       name="zshared", shared_with_workspace=True),
        secrets={"api_key": "k"})
    store.upsert_provider_config(
        ProviderConfig(kind="image", created_by=me, workspace_id=ws, name="mine"),
        secrets={"api_key": "k"})
    got = visible_provider_configs(store, "image", me, {ws}, is_owner=False)
    names = [c.name for c in got]
    assert names == ["mine", "zshared"]   # own first, then the workspace-shared one
    assert is_owned(got[0], me)


# --- the Instruction picks round-trip, including the Azure whitelist ----------------- #

@_ID
def test_instruction_carries_the_provider_picks(label, store):
    store.init()
    instr = store.upsert_instruction(Instruction(
        name="I", image_config_id="img-1", video_config_id="vid-1"))
    got = store.get_instruction(instr.id)
    assert got.image_config_id == "img-1"
    assert got.video_config_id == "vid-1"


# --- runtime threading: no global mutation ------------------------------------------ #

def test_image_tool_uses_state_settings_and_caches_per_config():
    """``perform_generate_image`` reads ``state['image_settings']`` and builds a
    client per distinct connection — a different config gets a different client,
    and no SDK global is touched."""
    from aismm.tools import image_tool

    a = ImageSettings(api_key="k1", endpoint="https://a", model="gpt-image-2")
    b = ImageSettings(api_key="k2", endpoint="https://b", model="gpt-image-2")
    image_tool._clients.clear()
    ca1 = image_tool._client_for(a)
    ca2 = image_tool._client_for(a)
    cb = image_tool._client_for(b)
    assert ca1 is ca2            # same config reuses the client
    assert cb is not ca1         # a different config gets its own client


def test_generate_image_tool_is_disabled_when_no_image_settings():
    """The factory gates on the resolved image connection: an empty one (a run
    whose selection was inaccessible) leaves the tool absent, not a fallback."""
    from aismm.tools import image_tool
    state = {"image_settings": ImageSettings()}   # not enabled (no key/endpoint)
    assert image_tool._make_generate_image(state) is None
    state = {"image_settings": ImageSettings(api_key="k", endpoint="https://e")}
    assert image_tool._make_generate_image(state) is not None


# --- the dashboard renders the image/video managers and admin summary --------------- #

@pytest.fixture()
def _owner_app(store, monkeypatch, tmp_path):
    import dataclasses

    from aismm import assets as assets_module
    from aismm.config import AuthSettings
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    (tmp_path / "assets").mkdir(exist_ok=True)

    def make(auth):
        patched = dataclasses.replace(config_module.settings, auth=auth, data_dir=tmp_path)
        for module in (sso, app_module, config_module, assets_module):
            monkeypatch.setattr(module, "settings", patched)
        monkeypatch.setattr(app_module, "get_store", lambda: store)
        application = app_module.create_app()
        application.secret_key = "test"
        return application

    return make


def test_settings_page_renders_image_and_video_managers(_owner_app):
    from aismm.config import AuthSettings
    client = _owner_app(AuthSettings()).test_client()   # SSO off ⇒ local owner
    body = client.get("/settings").get_data(as_text=True)
    assert "Image connections" in body
    assert "Video (Sora) connections" in body
    # The instruction form offers the two pickers.
    form = client.get("/instructions/new").get_data(as_text=True)
    assert 'name="image_config_id"' in form
    assert 'name="video_config_id"' in form


def test_admin_summary_lists_provider_connections(_owner_app):
    from aismm.config import AuthSettings
    client = _owner_app(AuthSettings()).test_client()
    body = client.get("/admin").get_data(as_text=True)
    assert "Image &amp; video connections" in body


def test_sora_config_follows_the_active_contextvar():
    """The per-run Sora pool is read from the ContextVar; resetting the token
    restores the deployment default — this is the per-run isolation the scheduler
    relies on."""
    from aismm.tools import sora_config

    pool = SoraSettings(endpoints=["https://x.openai.azure.com"], keys=["k"],
                        models=["sora-2"])
    token = sora_config._ACTIVE.set(pool)
    try:
        assert [r["endpoint"] for r in sora_config.pool()] == [
            "https://x.openai.azure.com"]
        assert sora_config.enabled()
    finally:
        sora_config._ACTIVE.reset(token)
    # After reset the active pool falls back to settings.sora (unconfigured here).
    assert sora_config._ACTIVE.get() is None
