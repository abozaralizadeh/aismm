"""User-managed LLM connections + the site owner.

Covers the pieces the "bring-your-own-model + owner oversight" change added:

* ``LLMConfig`` / ``UserProfile`` round-trip through **both** store backends,
  and ``list_llm_configs`` never leaks a decrypted secret (Fernet stays inside
  the store, exactly like account tokens and ``PlatformApp`` credentials);
* ``resolve_llm_settings`` — env sentinel → ``settings.llm``, a real azure row
  decrypts, a disabled/missing row → ``None`` (no silent fallback);
* the ``can_select`` sharing matrix (own / workspace-shared / people-shared /
  env / disabled / denied);
* ``build_model_for`` builds a dedicated client and does **not** mutate the SDK
  default (concurrency safety);
* ``run_for_account`` fails a run with a clear ``no_llm`` message when nothing is
  accessible, rather than falling back to the deployment default;
* the owner gate: ``/admin`` is 404 for a non-owner and 200 for the owner, and
  SSO-off makes the single local operator the owner.
"""
import dataclasses

import pytest

from aismm import config as config_module
from aismm.config import AuthSettings, LLMSettings
from aismm.llm_access import can_select, env_config, is_owned, visible_configs
from aismm.models import ENV_LLM_ID, Instruction, LLMConfig


def _both_backends():
    from aismm.store.azure_store import AzureStore
    from aismm.store.local_store import LocalStore
    from tests.test_azure_store import FakeTableClient

    return [
        ("local", LocalStore(db_url="sqlite:///:memory:")),
        ("azure", AzureStore(table_client=FakeTableClient())),
    ]


_SECRET = "sk-super-secret-key-value-123"


# --- store round-trip, both backends ---------------------------------------------------- #

@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_llm_config_round_trips_and_decrypts(label, store):
    """An azure connection persists its non-secret fields in both backends, and the
    secret round-trips ONLY through the decrypt path (``resolve_llm_settings``)."""
    store.init()
    cfg = store.upsert_llm_config(
        LLMConfig(workspace_id="w1", created_by="me@x.com", name="My Azure",
                  provider="azure", model="gpt-5",
                  azure_endpoint="https://ex.openai.azure.com",
                  azure_api_version="2025-04-01-preview"),
        azure_api_key=_SECRET)
    got = store.get_llm_config(cfg.id)
    assert got is not None
    assert got.name == "My Azure"
    assert got.provider == "azure"
    assert got.model == "gpt-5"
    assert got.azure_endpoint == "https://ex.openai.azure.com"
    assert got.created_by == "me@x.com"
    # The decrypt path returns usable, populated settings.
    llm = store.resolve_llm_settings(cfg.id)
    assert isinstance(llm, LLMSettings)
    assert llm.azure_api_key == _SECRET
    assert llm.model == "gpt-5"


@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_list_llm_configs_never_leaks_a_decrypted_secret(label, store):
    """Listing connections exposes names/metadata only — the plaintext key must
    live only behind ``resolve_llm_settings``."""
    store.init()
    store.upsert_llm_config(
        LLMConfig(workspace_id="w1", created_by="me@x.com", name="A",
                  provider="azure", model="m", azure_endpoint="https://e"),
        azure_api_key=_SECRET)
    for cfg in store.list_llm_configs():
        # No attribute of a listed config equals the plaintext secret.
        for value in vars(cfg).values():
            assert value != _SECRET
        assert _SECRET not in (cfg.azure_api_key_enc or "")


@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_upsert_preserves_secret_when_not_provided(label, store):
    """Editing a connection without re-entering the key must not wipe it — the
    form never echoes a secret back, so ``None`` means 'leave stored'."""
    store.init()
    cfg = store.upsert_llm_config(
        LLMConfig(created_by="me@x.com", name="A", provider="azure",
                  model="m", azure_endpoint="https://e"),
        azure_api_key=_SECRET)
    cfg.name = "Renamed"
    store.upsert_llm_config(cfg)  # no azure_api_key kwarg → preserve
    assert store.resolve_llm_settings(cfg.id).azure_api_key == _SECRET
    assert store.get_llm_config(cfg.id).name == "Renamed"


@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_list_llm_configs_scopes_by_workspace(label, store):
    store.init()
    store.upsert_llm_config(LLMConfig(workspace_id="w1", created_by="a", name="one",
                                      provider="azure", model="m"))
    store.upsert_llm_config(LLMConfig(workspace_id="w2", created_by="b", name="two",
                                      provider="azure", model="m"))
    assert {c.name for c in store.list_llm_configs()} == {"one", "two"}
    assert {c.name for c in store.list_llm_configs(workspace_id="w1")} == {"one"}


@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_delete_llm_config(label, store):
    store.init()
    cfg = store.upsert_llm_config(LLMConfig(created_by="a", name="gone",
                                            provider="azure", model="m"))
    store.delete_llm_config(cfg.id)
    assert store.get_llm_config(cfg.id) is None


@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_resolve_env_sentinel_is_the_deployment_llm(label, store):
    """The env row (and even a bare 'env' id before any row exists) resolves to
    the deployment ``settings.llm`` — never None, never a decrypt."""
    store.init()
    assert store.resolve_llm_settings(ENV_LLM_ID) == config_module.settings.llm


@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_resolve_disabled_and_missing_return_none(label, store):
    """A disabled connection or a deleted id must NOT silently fall back."""
    store.init()
    cfg = store.upsert_llm_config(
        LLMConfig(created_by="a", name="off", provider="azure", model="m",
                  azure_endpoint="https://e", enabled=False),
        azure_api_key=_SECRET)
    assert store.resolve_llm_settings(cfg.id) is None
    assert store.resolve_llm_settings("does-not-exist") is None


@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_instruction_llm_config_id_round_trips(label, store):
    """The picker column must survive both backends (Azure uses an explicit
    whitelist, so a dropped column would silently reset every instruction to the
    deployment default)."""
    store.init()
    instr = store.upsert_instruction(Instruction(name="I", llm_config_id="cfg-123"))
    assert store.get_instruction(instr.id).llm_config_id == "cfg-123"


# --- user profiles (login records for the Admin page) ----------------------------------- #

@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_record_login_round_trips(label, store):
    store.init()
    store.record_login("Me@Example.com", "Me")
    prof = store.get_user_profile("me@example.com") or store.get_user_profile("Me@Example.com")
    assert prof is not None
    assert prof.display_name == "Me"
    assert prof.last_login_at is not None
    assert prof.last_active_at is not None       # a login is also activity
    assert any(p.display_name == "Me" for p in store.list_user_profiles())


@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_record_activity_advances_only_last_active(label, store):
    """Ongoing use bumps last_active_at but must NOT overwrite last_login_at or
    the display name — the two answer different questions on the Admin page."""
    store.init()
    store.record_login("Me@Example.com", "Me")
    prof = store.get_user_profile("me@example.com")
    login_at = prof.last_login_at

    store.record_activity("Me@Example.com")
    prof = store.get_user_profile("me@example.com")
    assert prof.last_active_at >= login_at
    assert prof.last_login_at == login_at        # unchanged
    assert prof.display_name == "Me"             # not wiped by the activity write


@pytest.mark.parametrize("label,store", _both_backends(), ids=lambda v: v if isinstance(v, str) else "")
def test_record_activity_creates_a_profile_if_absent(label, store):
    """A person could be active before any login record exists (e.g. SSO-off) —
    the activity write must materialise the profile rather than no-op."""
    store.init()
    store.record_activity("New@Example.com")
    prof = store.get_user_profile("new@example.com")
    assert prof is not None and prof.last_active_at is not None


# --- can_select sharing matrix ---------------------------------------------------------- #

def _cfg(**kw):
    base = dict(id="c1", workspace_id="w1", created_by="owner@x.com",
                provider="azure", model="m", enabled=True)
    base.update(kw)
    return LLMConfig(**base)


def test_can_select_owner_of_the_config():
    cfg = _cfg(created_by="me@x.com")
    assert can_select(cfg, "me@x.com", set(), is_owner=False) is True
    assert is_owned(cfg, "me@x.com") is True


def test_can_select_workspace_share():
    cfg = _cfg(created_by="a@x.com", shared_with_workspace=True)
    # A member of the config's workspace may pick it...
    assert can_select(cfg, "member@x.com", {"w1"}, is_owner=False) is True
    # ...someone not in that workspace may not.
    assert can_select(cfg, "member@x.com", {"w9"}, is_owner=False) is False


def test_can_select_people_share():
    cfg = _cfg(created_by="a@x.com")
    cfg.set_shared_with(["friend@x.com"])
    assert can_select(cfg, "friend@x.com", set(), is_owner=False) is True
    assert can_select(cfg, "stranger@x.com", set(), is_owner=False) is False


def test_can_select_env_is_owner_only_unless_shared():
    env = env_config()  # created_by = "" when no owner configured
    # The site owner may always use the deployment default.
    assert can_select(env, "anyone@x.com", set(), is_owner=True) is True
    # A non-owner may not, unless the owner people-shared it.
    assert can_select(env, "anyone@x.com", set(), is_owner=False) is False
    env.set_shared_with(["anyone@x.com"])
    assert can_select(env, "anyone@x.com", set(), is_owner=False) is True


def test_can_select_disabled_is_never_selectable():
    cfg = _cfg(created_by="me@x.com", enabled=False)
    assert can_select(cfg, "me@x.com", {"w1"}, is_owner=True) is False


def test_can_select_denies_unrelated_private_config():
    cfg = _cfg(created_by="a@x.com")  # private, not shared
    assert can_select(cfg, "b@x.com", {"w1"}, is_owner=False) is False


def test_visible_configs_lists_own_first(store):
    store.init()
    store.upsert_llm_config(LLMConfig(workspace_id="w1", created_by="me@x.com",
                                      name="Mine", provider="azure", model="m"))
    shared = store.upsert_llm_config(LLMConfig(workspace_id="w1", created_by="other@x.com",
                                               name="Theirs", provider="azure", model="m",
                                               shared_with_workspace=True))
    got = visible_configs(store, "me@x.com", {"w1"}, is_owner=False)
    names = [c.name for c in got]
    assert names == ["Mine", "Theirs"]  # own first, then shared
    assert shared.id in {c.id for c in got}


# --- build_model_for does not mutate the SDK default ------------------------------------ #

def test_build_model_for_does_not_touch_the_sdk_default(monkeypatch):
    import aismm.llm as llm_module

    calls = []
    monkeypatch.setattr(llm_module, "set_default_openai_client",
                        lambda client: calls.append(client))
    llm = LLMSettings(provider="azure", model="gpt-5", azure_api_key="k",
                      azure_endpoint="https://e.openai.azure.com",
                      azure_api_version="2025-04-01-preview")
    model = llm_module.build_model_for(llm)
    assert model.model == "gpt-5"
    assert calls == []  # never registered as the global default


def test_build_model_for_reuses_a_client_per_fingerprint():
    import aismm.llm as llm_module

    llm = LLMSettings(provider="azure", model="gpt-5", azure_api_key="k",
                      azure_endpoint="https://e.openai.azure.com",
                      azure_api_version="2025-04-01-preview")
    a = llm_module.build_model_for(llm)
    b = llm_module.build_model_for(dataclasses.replace(llm))  # same fingerprint
    assert a is b


# --- run_for_account fails clearly with no accessible LLM ------------------------------- #

def test_run_for_account_fails_with_no_llm(store, monkeypatch):
    import asyncio

    from aismm.agent import manager_agent
    from aismm.models import Account, PlatformName, Run, RunStatus

    store.init()
    # A private connection owned by someone else, picked by the instruction.
    other = store.upsert_llm_config(LLMConfig(workspace_id="w-other", created_by="stranger@x.com",
                                              name="Not yours", provider="azure", model="m",
                                              azure_endpoint="https://e"))
    account = store.upsert_account(
        Account(platform=PlatformName.twitter, handle="h", external_id="1"),
        access_token="t")
    instr = store.upsert_instruction(
        Instruction(name="I", workspace_id="w-mine", llm_config_id=other.id))
    run = store.add_run(Run(instruction_id=instr.id, account_id=account.id,
                            platform=PlatformName.twitter, status=RunStatus.running))
    # SSO on so the creator is NOT automatically the owner.
    patched = dataclasses.replace(config_module.settings,
                                  auth=AuthSettings(owner_emails=["owner@x.com"], enabled_override=True))
    monkeypatch.setattr(manager_agent, "settings", patched)

    result = asyncio.run(manager_agent.run_for_account(account, instr, store, run))
    assert result["error"] == "no_llm"
    assert store.get_run(run.id).status is RunStatus.failed
    assert "LLM connection" in store.get_run(run.id).error


# --- the owner gate: /admin is 404 for a non-owner, 200 for the owner ------------------- #

@pytest.fixture()
def _owner_app(store, monkeypatch, tmp_path):
    """A dashboard app factory whose AuthSettings we control per test."""
    from aismm import assets as assets_module
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


def test_admin_page_is_hidden_from_non_owner(_owner_app):
    from aismm.dashboard import sso

    auth = AuthSettings(issuer="https://iss", client_id="c", client_secret="s",
                        owner_emails=["owner@x.com"], allowed_emails=["nobody@x.com"])
    client = _owner_app(auth).test_client()
    with client.session_transaction() as sess:
        sess[sso._SESSION_USER] = {"email": "nobody@x.com", "name": "Nobody"}
    assert client.get("/admin").status_code == 404


def test_admin_page_is_reachable_by_the_owner(_owner_app):
    from aismm.dashboard import sso

    auth = AuthSettings(issuer="https://iss", client_id="c", client_secret="s",
                        owner_emails=["owner@x.com"], allowed_emails=["owner@x.com"])
    client = _owner_app(auth).test_client()
    with client.session_transaction() as sess:
        sess[sso._SESSION_USER] = {"email": "owner@x.com", "name": "Owner"}
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert b"Admin" in resp.data


def test_admin_page_is_open_when_sso_is_off(_owner_app):
    """SSO off ⇒ one local operator owns everything, including Admin."""
    client = _owner_app(AuthSettings()).test_client()
    assert client.get("/admin").status_code == 200
