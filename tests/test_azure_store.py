"""AzureStore against a fake Table client — no storage account, no network.

The fake mimics the parts of ``TableClient`` this store uses, including the two
behaviours the lock depends on: ``create_entity`` raising ``ResourceExistsError``
on a duplicate key, and PartitionKey filtering.
"""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from aismm import config as config_module
from aismm.config import AzureStorageSettings
from aismm.models import (
    Account, Instruction, MediaPref, PlatformName, PublishMode, Run, RunStatus, StagedPost,
    StagedStatus,
)
from aismm.store.azure_store import AzureStore


class FakeTableClient:
    """Minimal in-memory stand-in for azure.data.tables.TableClient."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            raise ResourceExistsError("EntityAlreadyExists")
        self.rows[key] = dict(entity)

    def upsert_entity(self, entity, mode=None):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        try:
            return dict(self.rows[(partition_key, row_key)])
        except KeyError:
            raise ResourceNotFoundError("not found") from None

    def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)

    def query_entities(self, query_filter, **kwargs):
        wanted = query_filter.split("'")[1]
        return [dict(v) for (pk, _), v in self.rows.items() if pk == wanted]


@pytest.fixture()
def store():
    return AzureStore(table_client=FakeTableClient())


# --- accounts (and token encryption) --------------------------------------------- #

def test_account_round_trip(store):
    account = Account(platform=PlatformName.instagram, handle="me", external_id="ig1")
    store.upsert_account(account, access_token="secret-token", refresh_token="refresh-token")

    loaded = store.get_account(account.id)
    assert loaded.platform is PlatformName.instagram
    assert loaded.handle == "me"
    assert store.get_tokens(account.id) == ("secret-token", "refresh-token")


def test_tokens_are_encrypted_at_rest(store):
    """The storage account must never hold a usable token."""
    account = Account(platform=PlatformName.twitter)
    store.upsert_account(account, access_token="plaintext-value")
    raw = store._table.rows[("account", account.id)]
    assert "plaintext-value" not in str(raw)
    assert raw["access_token_enc"]


def test_upserting_without_tokens_keeps_the_stored_ones(store):
    account = Account(platform=PlatformName.twitter)
    store.upsert_account(account, access_token="keep-me")
    reloaded = store.get_account(account.id)
    reloaded.access_token_enc = ""          # simulate a caller that dropped it
    store.upsert_account(reloaded)
    assert store.get_tokens(account.id)[0] == "keep-me"


def test_missing_account_is_none(store):
    assert store.get_account("nope") is None
    assert store.get_tokens("nope") == ("", "")


def test_accounts_are_listed_in_creation_order(store):
    older = Account(platform=PlatformName.twitter,
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = Account(platform=PlatformName.tiktok,
                    created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    store.upsert_account(newer)
    store.upsert_account(older)
    assert [a.id for a in store.list_accounts()] == [older.id, newer.id]


def test_delete_account(store):
    account = Account(platform=PlatformName.youtube)
    store.upsert_account(account)
    store.delete_account(account.id)
    assert store.get_account(account.id) is None


# --- instructions ----------------------------------------------------------------- #

def test_instruction_round_trip_preserves_enums_and_lists(store):
    instr = Instruction(name="Daily", brief="b", schedule="0 9 * * *",
                        publish_mode=PublishMode.approval, media_pref=MediaPref.video)
    instr.set_account_ids(["a1", "a2"])
    store.upsert_instruction(instr)

    loaded = store.get_instruction(instr.id)
    assert loaded.publish_mode is PublishMode.approval
    assert loaded.media_pref is MediaPref.video
    assert loaded.account_ids == ["a1", "a2"]
    assert loaded.schedule == "0 9 * * *"


def test_enabled_only_filter(store):
    on = Instruction(name="on", enabled=True)
    off = Instruction(name="off", enabled=False)
    store.upsert_instruction(on)
    store.upsert_instruction(off)
    assert [i.name for i in store.list_instructions(enabled_only=True)] == ["on"]
    assert len(store.list_instructions()) == 2


def test_oversized_property_fails_loudly(store):
    """Better a clear error than an opaque Azure 400 at publish time."""
    instr = Instruction(name="big", brief="x" * 40_000)
    with pytest.raises(ValueError, match="caps a property"):
        store.upsert_instruction(instr)


# --- instruction state ------------------------------------------------------------- #

def test_memory_and_note_round_trip(store):
    instr = store.upsert_instruction(Instruction(name="crawl"))
    store.set_memory(instr.id, "CURRENT POSITION: 2026-03-14")
    store.set_note(instr.id, "Be more current.")
    state = store.get_state(instr.id)
    assert state.memory == "CURRENT POSITION: 2026-03-14"
    assert state.note == "Be more current."
    assert state.memory_updated_at and state.note_updated_at


def test_state_defaults_when_absent(store):
    state = store.get_state("unknown")
    assert state.memory == "" and state.note == "" and state.compactions == 0


def test_compaction_counter(store):
    instr = store.upsert_instruction(Instruction(name="crawl"))
    store.set_memory(instr.id, "a")
    store.set_memory(instr.id, "b", compacted=True)
    assert store.get_state(instr.id).compactions == 1


def test_deleting_an_instruction_drops_its_state(store):
    instr = store.upsert_instruction(Instruction(name="crawl"))
    store.set_memory(instr.id, "position")
    store.delete_instruction(instr.id)
    assert store.get_state(instr.id).memory == ""


# --- runs and staged posts ---------------------------------------------------------- #

def test_runs_are_listed_newest_first_and_limited(store):
    for day in range(1, 6):
        store.add_run(Run(instruction_id="i", account_id="a",
                          created_at=datetime(2026, 3, day, tzinfo=timezone.utc)))
    runs = store.list_runs(limit=3)
    assert len(runs) == 3
    assert runs[0].created_at.day == 5


def test_run_update_persists_status_and_url(store):
    run = store.add_run(Run(instruction_id="i", account_id="a"))
    run.status = RunStatus.published
    run.external_url = "https://example.com/p/1"
    store.update_run(run)
    reloaded = [r for r in store.list_runs() if r.id == run.id][0]
    assert reloaded.status is RunStatus.published
    assert reloaded.external_url == "https://example.com/p/1"


def test_pending_only_staged_filter(store):
    store.add_staged(StagedPost(instruction_id="i", account_id="a",
                                status=StagedStatus.preview))
    pending = store.add_staged(StagedPost(instruction_id="i", account_id="a",
                                          status=StagedStatus.pending_approval))
    listed = store.list_staged(pending_only=True)
    assert [s.id for s in listed] == [pending.id]


def test_staged_update_round_trip(store):
    staged = store.add_staged(StagedPost(instruction_id="i", account_id="a",
                                         media_kind="video"))
    staged.status = StagedStatus.published
    store.update_staged(staged)
    assert store.get_staged(staged.id).status is StagedStatus.published


# --- locks (the SandBox _try_acquire_lock scheme) ------------------------------------ #

def test_lock_is_single_flight(store):
    assert store.acquire_lock("instr:1:acct:2") is True
    assert store.acquire_lock("instr:1:acct:2") is False


def test_lock_is_released(store):
    store.acquire_lock("k")
    store.release_lock("k")
    assert store.acquire_lock("k") is True


def test_stale_lock_is_reclaimed(store):
    """A crashed run must not hold its lock forever."""
    store.acquire_lock("k", ttl_seconds=1800)
    store._table.rows[("lock", "k")]["acquired_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert store.acquire_lock("k", ttl_seconds=1800) is True


def test_fresh_lock_is_not_reclaimed(store):
    store.acquire_lock("k", ttl_seconds=1800)
    assert store.acquire_lock("k", ttl_seconds=1800) is False


def test_lock_keys_are_sanitized_for_rowkey(store):
    """Lock keys contain ':' and could contain '/', which RowKey forbids."""
    assert store.acquire_lock("instr:a/b#c?d") is True
    stored = [rk for (pk, rk) in store._table.rows if pk == "lock"][0]
    assert "/" not in stored and "#" not in stored and "?" not in stored


# --- backend selection --------------------------------------------------------------- #

def _settings(**kwargs):
    return dataclasses.replace(config_module.settings, **kwargs)


def test_azure_selected_automatically_when_configured():
    s = _settings(store_backend="auto",
                  azure_storage=AzureStorageSettings(connection_string="UseDevelopmentStorage=true"))
    assert s.use_azure_store is True


def test_local_is_used_without_a_connection_string():
    assert _settings(store_backend="auto",
                     azure_storage=AzureStorageSettings()).use_azure_store is False


def test_backend_can_be_forced_either_way():
    configured = AzureStorageSettings(connection_string="x")
    assert _settings(azure_storage=configured, store_backend="local").use_azure_store is False
    assert _settings(store_backend="azure").use_azure_store is True


def test_azure_store_refuses_to_start_unconfigured():
    """conftest pins an empty connection string, so this must fail clearly."""
    with pytest.raises(RuntimeError, match="AZURE_STORAGE_CONNECTION_STRING"):
        AzureStore()


# --- entity shape (what Azure will actually accept) ---------------------------------- #

def test_stored_values_are_table_safe_primitives(store):
    """Table Storage rejects dicts/lists/None and is fussy about datetimes.

    Everything must be str/int/float/bool by the time it reaches the SDK, or the
    first real write fails with an opaque 400.
    """
    account = Account(platform=PlatformName.instagram,
                      expires_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
    store.upsert_account(account, access_token="t")
    instr = Instruction(name="n", brief="b", publish_mode=PublishMode.live)
    instr.set_account_ids(["a1"])
    store.upsert_instruction(instr)
    store.add_run(Run(instruction_id=instr.id, account_id=account.id))
    store.add_staged(StagedPost(instruction_id=instr.id, account_id=account.id))
    store.set_memory(instr.id, "m")
    store.acquire_lock("k")

    for key, entity in store._table.rows.items():
        for field, value in entity.items():
            assert isinstance(value, (str, int, float, bool)), (
                f"{key}.{field} is {type(value).__name__}, which Table Storage rejects")


def test_datetimes_round_trip_through_iso_strings(store):
    when = datetime(2026, 3, 14, 9, 30, tzinfo=timezone.utc)
    account = Account(platform=PlatformName.twitter, expires_at=when, created_at=when)
    store.upsert_account(account)
    loaded = store.get_account(account.id)
    assert loaded.expires_at == when
    assert loaded.created_at == when
