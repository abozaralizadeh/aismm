"""Azure Table Storage implementation of :class:`~aismm.store.base.Store`.

Follows the SandBox convention exactly (``GenBox/azurestorage.py``,
``ComicBook/azurestorage.py``): **one table per project**, with the entity type
in the ``PartitionKey`` and the entity id in the ``RowKey``. So a single table —
``aismm`` by default — holds accounts, instructions, runs, staged posts,
per-instruction state and locks, and provisioning is one table plus one blob
container.

    PartitionKey   RowKey                     what
    ------------   ------------------------   ---------------------------------
    account        <account id>               connected social profile
    instruction    <instruction id>           brief + schedule + publish mode
    run            <run id>                   one execution
    staged         <staged id>                preview / approval queue item
    state          <instruction id>           carry-over memory + operator note
    lock           <lock key, sanitized>      single-flight lock

Notes that shaped this implementation:

* **Tokens stay encrypted.** Fernet applies exactly as in the SQLite store — the
  ``*_enc`` fields cross this boundary already-encrypted, so the storage account
  never holds a usable token.
* **Table Storage cannot sort or paginate server-side**, so lists are sorted and
  trimmed in Python — the same thing ``GenBox.get_last_n_rows`` does.
* **Datetimes are stored as ISO strings.** The SDK's native datetime support
  round-trips through UTC with second precision and its own tz handling; ISO
  strings are lossless and match the lock timestamps SandBox writes.
* **A property caps at 64 KB.** ``MAX_PROPERTY_CHARS`` guards the two free-text
  fields that could realistically grow (brief, memory) with a clear error rather
  than an opaque Azure 400.
* **Locks are ``create_entity`` + ``ResourceExistsError``**, with a TTL reclaim —
  ``GenBox._try_acquire_lock``, ported verbatim in spirit.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from ..crypto import decrypt, encrypt
from ..models import (
    Account, Instruction, InstructionState, MediaPref, PlatformApp, PlatformName, PublishMode,
    Run, RunStatus, StagedPost, StagedStatus,
)
from .base import Store

logger = logging.getLogger("aismm.store.azure")

PK_ACCOUNT = "account"
PK_INSTRUCTION = "instruction"
PK_RUN = "run"
PK_STAGED = "staged"
PK_STATE = "state"
PK_APP = "app"
PK_LOCK = "lock"

# Azure caps a single string property at 64 KB; ComicBook uses a similar guard.
MAX_PROPERTY_CHARS = 32_000

_DATETIME_FIELDS = {
    "expires_at", "created_at", "updated_at", "memory_updated_at", "note_updated_at",
    "acquired_at",
}
# RowKey forbids / \ # ? and control chars — lock keys contain ':'.
_ROWKEY_UNSAFE = re.compile(r"[/\\#?\x00-\x1f\x7f-\x9f]")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_key(value: str) -> str:
    return _ROWKEY_UNSAFE.sub("_", value or "")


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _parse_dt(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _check_size(entity: dict) -> None:
    for key, value in entity.items():
        if isinstance(value, str) and len(value) > MAX_PROPERTY_CHARS:
            raise ValueError(
                f"'{key}' is {len(value)} characters; Azure Table Storage caps a property at "
                f"{MAX_PROPERTY_CHARS}. Shorten it (or move that field to blob storage)."
            )


class AzureStore(Store):
    """Table-backed store. Pass ``table_client`` to inject a fake in tests."""

    def __init__(self, table_client=None) -> None:
        self._table = table_client
        if table_client is None:
            self.init()

    # --- plumbing ---------------------------------------------------------- #
    def init(self) -> None:
        """Create the table if missing and cache its client."""
        if self._table is not None:
            return
        from azure.data.tables import TableServiceClient

        from ..config import settings

        cfg = settings.azure_storage
        if not cfg.configured:
            raise RuntimeError(
                "Azure storage is not configured. Set AZURE_STORAGE_CONNECTION_STRING "
                "(or SandBox's `connection_string`) in your .env."
            )
        service = TableServiceClient.from_connection_string(conn_str=cfg.connection_string)
        try:
            service.create_table(cfg.table_name)
            logger.info("Created table %s", cfg.table_name)
        except Exception:  # noqa: BLE001 - already exists is the normal case
            pass
        self._table = service.get_table_client(cfg.table_name)

    @property
    def table(self):
        if self._table is None:
            self.init()
        return self._table

    def _upsert(self, partition: str, row: str, payload: dict) -> None:
        from azure.data.tables import UpdateMode

        entity = {"PartitionKey": partition, "RowKey": _safe_key(row)}
        entity.update({k: _iso(v) for k, v in payload.items() if v is not None})
        _check_size(entity)
        self.table.upsert_entity(entity=entity, mode=UpdateMode.REPLACE)

    def _get(self, partition: str, row: str):
        try:
            return self.table.get_entity(partition_key=partition, row_key=_safe_key(row))
        except Exception:  # noqa: BLE001 - ResourceNotFoundError and friends
            return None

    def _query(self, partition: str) -> list[dict]:
        return list(self.table.query_entities(query_filter=f"PartitionKey eq '{partition}'"))

    def _delete(self, partition: str, row: str) -> None:
        try:
            self.table.delete_entity(partition_key=partition, row_key=_safe_key(row))
        except Exception:  # noqa: BLE001 - absent is fine
            pass

    # --- model <-> entity -------------------------------------------------- #
    @staticmethod
    def _account_to_entity(a: Account) -> dict:
        return {
            "platform": a.platform.value, "handle": a.handle, "external_id": a.external_id,
            "access_token_enc": a.access_token_enc, "refresh_token_enc": a.refresh_token_enc,
            "expires_at": a.expires_at, "meta_json": a.meta_json, "created_at": a.created_at,
        }

    @staticmethod
    def _account_from_entity(e) -> Account:
        return Account(
            id=e["RowKey"], platform=PlatformName(e["platform"]), handle=e.get("handle", ""),
            external_id=e.get("external_id", ""),
            access_token_enc=e.get("access_token_enc", ""),
            refresh_token_enc=e.get("refresh_token_enc", ""),
            expires_at=_parse_dt(e.get("expires_at")), meta_json=e.get("meta_json", "{}"),
            created_at=_parse_dt(e.get("created_at")) or _now(),
        )

    @staticmethod
    def _instruction_to_entity(i: Instruction) -> dict:
        return {
            "name": i.name, "brief": i.brief, "account_ids_json": i.account_ids_json,
            "schedule": i.schedule, "publish_mode": i.publish_mode.value,
            "media_pref": i.media_pref.value, "enabled": i.enabled,
            "created_at": i.created_at, "updated_at": i.updated_at,
        }

    @staticmethod
    def _instruction_from_entity(e) -> Instruction:
        return Instruction(
            id=e["RowKey"], name=e.get("name", ""), brief=e.get("brief", ""),
            account_ids_json=e.get("account_ids_json", "[]"), schedule=e.get("schedule", ""),
            publish_mode=PublishMode(e.get("publish_mode", "dry_run")),
            media_pref=MediaPref(e.get("media_pref", "auto")),
            enabled=bool(e.get("enabled", True)),
            created_at=_parse_dt(e.get("created_at")) or _now(),
            updated_at=_parse_dt(e.get("updated_at")) or _now(),
        )

    @staticmethod
    def _run_to_entity(r: Run) -> dict:
        return {
            "instruction_id": r.instruction_id, "account_id": r.account_id,
            "status": r.status.value, "caption": r.caption, "asset_path": r.asset_path,
            "external_url": r.external_url, "error": r.error, "log": r.log,
            "created_at": r.created_at,
        }

    @staticmethod
    def _run_from_entity(e) -> Run:
        return Run(
            id=e["RowKey"], instruction_id=e.get("instruction_id", ""),
            account_id=e.get("account_id", ""), status=RunStatus(e.get("status", "running")),
            caption=e.get("caption", ""), asset_path=e.get("asset_path", ""),
            external_url=e.get("external_url", ""), error=e.get("error", ""),
            log=e.get("log", ""), created_at=_parse_dt(e.get("created_at")) or _now(),
        )

    @staticmethod
    def _staged_to_entity(s: StagedPost) -> dict:
        return {
            "instruction_id": s.instruction_id, "account_id": s.account_id, "run_id": s.run_id,
            "caption": s.caption, "asset_path": s.asset_path, "media_kind": s.media_kind,
            "status": s.status.value, "external_url": s.external_url, "created_at": s.created_at,
        }

    @staticmethod
    def _staged_from_entity(e) -> StagedPost:
        return StagedPost(
            id=e["RowKey"], instruction_id=e.get("instruction_id", ""),
            account_id=e.get("account_id", ""), run_id=e.get("run_id", ""),
            caption=e.get("caption", ""), asset_path=e.get("asset_path", ""),
            media_kind=e.get("media_kind", "text"),
            status=StagedStatus(e.get("status", "preview")),
            external_url=e.get("external_url", ""),
            created_at=_parse_dt(e.get("created_at")) or _now(),
        )

    # --- accounts ---------------------------------------------------------- #
    def upsert_account(self, account, *, access_token=None, refresh_token=None):
        existing = self._get(PK_ACCOUNT, account.id)
        if access_token is not None:
            account.access_token_enc = encrypt(access_token)
        elif existing is not None:
            account.access_token_enc = account.access_token_enc or existing.get(
                "access_token_enc", "")
        if refresh_token is not None:
            account.refresh_token_enc = encrypt(refresh_token)
        elif existing is not None:
            account.refresh_token_enc = account.refresh_token_enc or existing.get(
                "refresh_token_enc", "")
        self._upsert(PK_ACCOUNT, account.id, self._account_to_entity(account))
        return account

    def get_account(self, account_id):
        entity = self._get(PK_ACCOUNT, account_id)
        return self._account_from_entity(entity) if entity else None

    def list_accounts(self):
        accounts = [self._account_from_entity(e) for e in self._query(PK_ACCOUNT)]
        return sorted(accounts, key=lambda a: a.created_at)

    def delete_account(self, account_id):
        self._delete(PK_ACCOUNT, account_id)

    def get_tokens(self, account_id):
        account = self.get_account(account_id)
        if not account:
            return "", ""
        return decrypt(account.access_token_enc), decrypt(account.refresh_token_enc)

    # --- platform apps ------------------------------------------------------ #
    def upsert_platform_app(self, app, *, client_secret=None):
        existing = self._get(PK_APP, app.id)
        if client_secret:
            app.client_secret_enc = encrypt(client_secret)
        elif existing is not None:
            app.client_secret_enc = app.client_secret_enc or existing.get("client_secret_enc", "")
        self._upsert(PK_APP, app.id, {
            "platform": app.platform.value, "name": app.name, "client_id": app.client_id,
            "client_secret_enc": app.client_secret_enc, "extra_json": app.extra_json,
            "enabled": app.enabled, "created_at": app.created_at,
        })
        return app

    @staticmethod
    def _app_from_entity(e) -> PlatformApp:
        return PlatformApp(
            id=e["RowKey"], platform=PlatformName(e["platform"]), name=e.get("name", ""),
            client_id=e.get("client_id", ""),
            client_secret_enc=e.get("client_secret_enc", ""),
            extra_json=e.get("extra_json", "{}"), enabled=bool(e.get("enabled", True)),
            created_at=_parse_dt(e.get("created_at")) or _now(),
        )

    def get_platform_app(self, app_id):
        entity = self._get(PK_APP, app_id)
        return self._app_from_entity(entity) if entity else None

    def list_platform_apps(self, platform=None):
        apps = [self._app_from_entity(e) for e in self._query(PK_APP)]
        if platform is not None:
            apps = [a for a in apps if a.platform == platform]
        return sorted(apps, key=lambda a: a.created_at)

    def delete_platform_app(self, app_id):
        self._delete(PK_APP, app_id)

    def get_app_secret(self, app_id):
        app = self.get_platform_app(app_id)
        return decrypt(app.client_secret_enc) if app else ""

    # --- instructions ------------------------------------------------------ #
    def upsert_instruction(self, instruction):
        instruction.updated_at = _now()
        self._upsert(PK_INSTRUCTION, instruction.id, self._instruction_to_entity(instruction))
        return instruction

    def get_instruction(self, instruction_id):
        entity = self._get(PK_INSTRUCTION, instruction_id)
        return self._instruction_from_entity(entity) if entity else None

    def list_instructions(self, *, enabled_only=False):
        items = [self._instruction_from_entity(e) for e in self._query(PK_INSTRUCTION)]
        if enabled_only:
            items = [i for i in items if i.enabled]
        return sorted(items, key=lambda i: i.created_at)

    def delete_instruction(self, instruction_id):
        self._delete(PK_INSTRUCTION, instruction_id)
        self._delete(PK_STATE, instruction_id)      # don't orphan memory/notes

    # --- instruction state (agent memory + human note) --------------------- #
    def get_state(self, instruction_id):
        entity = self._get(PK_STATE, instruction_id)
        if not entity:
            return InstructionState(instruction_id=instruction_id)
        return InstructionState(
            instruction_id=instruction_id, memory=entity.get("memory", ""),
            note=entity.get("note", ""),
            memory_updated_at=_parse_dt(entity.get("memory_updated_at")),
            note_updated_at=_parse_dt(entity.get("note_updated_at")),
            compactions=int(entity.get("compactions", 0) or 0),
        )

    def _put_state(self, state: InstructionState) -> InstructionState:
        self._upsert(PK_STATE, state.instruction_id, {
            "memory": state.memory, "note": state.note,
            "memory_updated_at": state.memory_updated_at,
            "note_updated_at": state.note_updated_at,
            "compactions": state.compactions,
        })
        return state

    def set_memory(self, instruction_id, memory, *, compacted=False):
        state = self.get_state(instruction_id)
        state.memory = memory or ""
        state.memory_updated_at = _now()
        if compacted:
            state.compactions += 1
        return self._put_state(state)

    def set_note(self, instruction_id, note):
        state = self.get_state(instruction_id)
        state.note = note or ""
        state.note_updated_at = _now()
        return self._put_state(state)

    # --- runs -------------------------------------------------------------- #
    def add_run(self, run):
        self._upsert(PK_RUN, run.id, self._run_to_entity(run))
        return run

    def update_run(self, run):
        self._upsert(PK_RUN, run.id, self._run_to_entity(run))
        return run

    def list_runs(self, *, limit=100):
        runs = [self._run_from_entity(e) for e in self._query(PK_RUN)]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[:limit]

    # --- staged posts ------------------------------------------------------ #
    def add_staged(self, staged):
        self._upsert(PK_STAGED, staged.id, self._staged_to_entity(staged))
        return staged

    def get_staged(self, staged_id):
        entity = self._get(PK_STAGED, staged_id)
        return self._staged_from_entity(entity) if entity else None

    def update_staged(self, staged):
        self._upsert(PK_STAGED, staged.id, self._staged_to_entity(staged))
        return staged

    def list_staged(self, *, pending_only=False, limit=100):
        items = [self._staged_from_entity(e) for e in self._query(PK_STAGED)]
        if pending_only:
            items = [s for s in items if s.status == StagedStatus.pending_approval]
        items.sort(key=lambda s: s.created_at, reverse=True)
        return items[:limit]

    # --- single-flight locks ---------------------------------------------- #
    def acquire_lock(self, key: str, ttl_seconds: int = 3600) -> bool:
        """Atomic ``create_entity``; ``ResourceExistsError`` means held.

        A lock older than ``ttl_seconds`` is stale and reclaimed — the same
        scheme as ``GenBox._try_acquire_lock``.
        """
        from azure.core.exceptions import ResourceExistsError

        entity = {"PartitionKey": PK_LOCK, "RowKey": _safe_key(key),
                  "acquired_at": _now().isoformat()}
        try:
            self.table.create_entity(entity=entity)
            return True
        except ResourceExistsError:
            existing = self._get(PK_LOCK, key)
            acquired = _parse_dt(existing.get("acquired_at")) if existing else None
            if acquired is None or acquired > _now() - timedelta(seconds=ttl_seconds):
                return False        # held and fresh (or unreadable — stay safe)
            # Stale: reclaim.
            self._delete(PK_LOCK, key)
            try:
                self.table.create_entity(entity=entity)
                return True
            except Exception:  # noqa: BLE001 - someone else won the race
                return False

    def release_lock(self, key: str) -> None:
        self._delete(PK_LOCK, key)
