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
    Account, AttachmentPurpose, Instruction, InstructionFile, InstructionState, InstructionTask,
    MediaPref, PlatformApp, PlatformName, PublishMode, Run, RunStatus, StagedPost, StagedStatus,
    Workspace, WorkspaceMember, WorkspaceRole,
)
from .base import Store

logger = logging.getLogger("aismm.store.azure")

PK_ACCOUNT = "account"
PK_INSTRUCTION = "instruction"
PK_RUN = "run"
PK_STAGED = "staged"
PK_STATE = "state"
PK_APP = "app"
PK_FILE = "file"
PK_LOCK = "lock"
PK_WORKSPACE = "workspace"
PK_MEMBER = "member"

# Azure caps a single string property at 64 KB; ComicBook uses a similar guard.
MAX_PROPERTY_CHARS = 32_000

_DATETIME_FIELDS = {
    "expires_at", "created_at", "updated_at", "memory_updated_at", "note_updated_at",
    "acquired_at",
}
# RowKey forbids / \ # ? and control chars — lock keys contain ':'.
_ROWKEY_UNSAFE = re.compile(r"[/\\#?\x00-\x1f\x7f-\x9f]")


def _ws_match(value: str, workspace_id) -> bool:
    """Does this row's workspace_id match the requested one (or ones)?

    Several ids is how the default workspace also claims rows written before
    workspaces existed — see LocalStore._ws_clause for the reasoning.
    """
    if isinstance(workspace_id, str):
        return value == workspace_id
    return value in set(workspace_id)


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
            "workspace_id": a.workspace_id,
            "platform": a.platform.value, "handle": a.handle, "external_id": a.external_id,
            "access_token_enc": a.access_token_enc, "refresh_token_enc": a.refresh_token_enc,
            "expires_at": a.expires_at, "meta_json": a.meta_json, "created_at": a.created_at,
        }

    @staticmethod
    def _account_from_entity(e) -> Account:
        return Account(
            id=e["RowKey"], workspace_id=e.get("workspace_id", ""),
            platform=PlatformName(e["platform"]), handle=e.get("handle", ""),
            external_id=e.get("external_id", ""),
            access_token_enc=e.get("access_token_enc", ""),
            refresh_token_enc=e.get("refresh_token_enc", ""),
            expires_at=_parse_dt(e.get("expires_at")), meta_json=e.get("meta_json", "{}"),
            created_at=_parse_dt(e.get("created_at")) or _now(),
        )

    @staticmethod
    def _instruction_to_entity(i: Instruction) -> dict:
        return {
            "workspace_id": i.workspace_id,
            "name": i.name, "brief": i.brief, "account_ids_json": i.account_ids_json,
            "schedule": i.schedule, "schedule_start_at": i.schedule_start_at,
            "tools_json": i.tools_json,
            "task_type": i.task_type.value,
            "publish_mode": i.publish_mode.value,
            "media_pref": i.media_pref.value, "enabled": i.enabled,
            "disclose_ai": i.disclose_ai,
            "created_at": i.created_at, "updated_at": i.updated_at,
        }

    @staticmethod
    def _instruction_from_entity(e) -> Instruction:
        return Instruction(
            id=e["RowKey"], workspace_id=e.get("workspace_id", ""),
            name=e.get("name", ""), brief=e.get("brief", ""),
            account_ids_json=e.get("account_ids_json", "[]"), schedule=e.get("schedule", ""),
            schedule_start_at=_parse_dt(e.get("schedule_start_at")),
            tools_json=e.get("tools_json", "[]"),
            task_type=InstructionTask(e.get("task_type", "publish")),
            publish_mode=PublishMode(e.get("publish_mode", "dry_run")),
            media_pref=MediaPref(e.get("media_pref", "auto")),
            enabled=bool(e.get("enabled", True)),
            disclose_ai=bool(e.get("disclose_ai", True)),
            created_at=_parse_dt(e.get("created_at")) or _now(),
            updated_at=_parse_dt(e.get("updated_at")) or _now(),
        )

    @staticmethod
    def _run_to_entity(r: Run) -> dict:
        return {
            "workspace_id": r.workspace_id,
            "instruction_id": r.instruction_id, "account_id": r.account_id,
            "status": r.status.value, "caption": r.caption, "asset_path": r.asset_path,
            "asset_paths_json": r.asset_paths_json, "placement": r.placement,
            "external_url": r.external_url, "error": r.error, "log": r.log,
            "prompt": r.prompt, "created_at": r.created_at,
        }

    @staticmethod
    def _run_from_entity(e) -> Run:
        return Run(
            id=e["RowKey"], workspace_id=e.get("workspace_id", ""),
            instruction_id=e.get("instruction_id", ""),
            account_id=e.get("account_id", ""), status=RunStatus(e.get("status", "running")),
            caption=e.get("caption", ""), asset_path=e.get("asset_path", ""),
            asset_paths_json=e.get("asset_paths_json", "[]"),
            placement=e.get("placement", "feed"),
            external_url=e.get("external_url", ""), error=e.get("error", ""),
            log=e.get("log", ""), prompt=e.get("prompt", ""),
            created_at=_parse_dt(e.get("created_at")) or _now(),
        )

    @staticmethod
    def _staged_to_entity(s: StagedPost) -> dict:
        return {
            "workspace_id": s.workspace_id,
            "instruction_id": s.instruction_id, "account_id": s.account_id, "run_id": s.run_id,
            "caption": s.caption, "asset_path": s.asset_path, "media_kind": s.media_kind,
            "asset_paths_json": s.asset_paths_json, "placement": s.placement,
            "action_type": s.action_type, "target_type": s.target_type,
            "target_id": s.target_id, "target_excerpt": s.target_excerpt,
            "status": s.status.value, "external_url": s.external_url, "created_at": s.created_at,
        }

    @staticmethod
    def _staged_from_entity(e) -> StagedPost:
        return StagedPost(
            id=e["RowKey"], workspace_id=e.get("workspace_id", ""),
            instruction_id=e.get("instruction_id", ""),
            account_id=e.get("account_id", ""), run_id=e.get("run_id", ""),
            caption=e.get("caption", ""), asset_path=e.get("asset_path", ""),
            media_kind=e.get("media_kind", "text"),
            asset_paths_json=e.get("asset_paths_json", "[]"),
            placement=e.get("placement", "feed"),
            action_type=e.get("action_type", "post"),
            target_type=e.get("target_type", ""),
            target_id=e.get("target_id", ""),
            target_excerpt=e.get("target_excerpt", ""),
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

    def list_accounts(self, *, workspace_id=None):
        accounts = [self._account_from_entity(e) for e in self._query(PK_ACCOUNT)]
        if workspace_id is not None:
            accounts = [a for a in accounts if _ws_match(a.workspace_id, workspace_id)]
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
            "workspace_id": app.workspace_id, "platform": app.platform.value,
            "name": app.name, "client_id": app.client_id,
            "client_secret_enc": app.client_secret_enc, "extra_json": app.extra_json,
            "enabled": app.enabled, "created_at": app.created_at,
        })
        return app

    @staticmethod
    def _app_from_entity(e) -> PlatformApp:
        return PlatformApp(
            id=e["RowKey"], workspace_id=e.get("workspace_id", ""),
            platform=PlatformName(e["platform"]), name=e.get("name", ""),
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

    def list_instructions(self, *, enabled_only=False, workspace_id=None):
        items = [self._instruction_from_entity(e) for e in self._query(PK_INSTRUCTION)]
        if workspace_id is not None:
            items = [i for i in items if _ws_match(i.workspace_id, workspace_id)]
        if enabled_only:
            items = [i for i in items if i.enabled]
        return sorted(items, key=lambda i: i.created_at)

    def delete_instruction(self, instruction_id):
        self._delete(PK_INSTRUCTION, instruction_id)
        self._delete(PK_STATE, instruction_id)      # don't orphan memory/notes
        for attachment in self.list_instruction_files(instruction_id):
            self._delete(PK_FILE, attachment.id)

    # --- instruction attachments ------------------------------------------- #
    def add_instruction_file(self, file):
        self._upsert(PK_FILE, file.id, {
            "instruction_id": file.instruction_id, "filename": file.filename,
            "content_type": file.content_type, "purpose": file.purpose.value,
            "asset_path": file.asset_path, "size_bytes": file.size_bytes,
            "text": file.text, "note": file.note, "created_at": file.created_at,
        })
        return file

    @staticmethod
    def _file_from_entity(e) -> InstructionFile:
        return InstructionFile(
            id=e["RowKey"], instruction_id=e.get("instruction_id", ""),
            filename=e.get("filename", ""), content_type=e.get("content_type", ""),
            purpose=AttachmentPurpose(e.get("purpose", "context")),
            asset_path=e.get("asset_path", ""), size_bytes=int(e.get("size_bytes", 0) or 0),
            text=e.get("text", ""), note=e.get("note", ""),
            created_at=_parse_dt(e.get("created_at")) or _now(),
        )

    def list_instruction_files(self, instruction_id):
        files = [self._file_from_entity(e) for e in self._query(PK_FILE)
                 if e.get("instruction_id") == instruction_id]
        return sorted(files, key=lambda f: f.created_at)

    def get_instruction_file(self, file_id):
        entity = self._get(PK_FILE, file_id)
        return self._file_from_entity(entity) if entity else None

    def delete_instruction_file(self, file_id):
        self._delete(PK_FILE, file_id)

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

    def get_run(self, run_id):
        entity = self._get(PK_RUN, run_id)
        return self._run_from_entity(entity) if entity else None

    def _matching_runs(self, *, status, instruction_id, account_id, search,
                       workspace_id=None):
        """Filter in Python — Table Storage has no LIKE and no server-side sort."""
        runs = [self._run_from_entity(e) for e in self._query(PK_RUN)]
        if status:
            wanted = status.value if hasattr(status, "value") else str(status)
            runs = [r for r in runs if r.status.value == wanted]
        if workspace_id is not None:
            runs = [r for r in runs if _ws_match(r.workspace_id, workspace_id)]
        if instruction_id:
            runs = [r for r in runs if r.instruction_id == instruction_id]
        if account_id:
            runs = [r for r in runs if r.account_id == account_id]
        term = (search or "").strip().lower()
        if term:
            # Match the instruction NAME too, which is what a human searches for.
            named = {i.id for i in self.list_instructions() if term in i.name.lower()}
            runs = [r for r in runs
                    if term in (r.caption or "").lower()
                    or term in (r.error or "").lower()
                    or term in (r.log or "").lower()
                    or term in (r.external_url or "").lower()
                    or r.instruction_id in named]
        return runs

    def list_runs(self, *, limit=100, offset=0, status=None, instruction_id=None,
                  account_id=None, workspace_id=None, search="", sort="created_at",
                  descending=True):
        runs = self._matching_runs(status=status, instruction_id=instruction_id,
                                   account_id=account_id, workspace_id=workspace_id,
                                   search=search)
        keys = {
            "created_at": lambda r: r.created_at,
            "status": lambda r: r.status.value,
            "instruction_id": lambda r: r.instruction_id,
            "account_id": lambda r: r.account_id,
        }
        runs.sort(key=keys.get(sort, keys["created_at"]), reverse=descending)
        return runs[offset:offset + limit]

    def count_runs(self, *, status=None, instruction_id=None, account_id=None,
                   workspace_id=None, search=""):
        return len(self._matching_runs(status=status, instruction_id=instruction_id,
                                       account_id=account_id, workspace_id=workspace_id,
                                       search=search))

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

    def list_staged(self, *, pending_only=False, limit=100, workspace_id=None):
        items = [self._staged_from_entity(e) for e in self._query(PK_STAGED)]
        if workspace_id is not None:
            items = [s for s in items if _ws_match(s.workspace_id, workspace_id)]
        if pending_only:
            items = [s for s in items if s.status == StagedStatus.pending_approval]
        items.sort(key=lambda s: s.created_at, reverse=True)
        return items[:limit]

    # --- workspaces -------------------------------------------------------- #
    def upsert_workspace(self, workspace):
        self._upsert(PK_WORKSPACE, workspace.id, {
            "name": workspace.name,
            "claims_unassigned": workspace.claims_unassigned,
            "created_by": workspace.created_by,
            "created_at": workspace.created_at,
        })
        return workspace

    @staticmethod
    def _workspace_from_entity(e) -> Workspace:
        return Workspace(
            id=e["RowKey"], name=e.get("name", ""),
            # auto_join is the pre-rename name of the same flag.
            claims_unassigned=bool(e.get("claims_unassigned", e.get("auto_join", False))),
            created_by=e.get("created_by", ""),
            created_at=_parse_dt(e.get("created_at")) or _now(),
        )

    def get_workspace(self, workspace_id):
        entity = self._get(PK_WORKSPACE, workspace_id)
        return self._workspace_from_entity(entity) if entity else None

    def list_workspaces(self):
        rows = [self._workspace_from_entity(e) for e in self._query(PK_WORKSPACE)]
        return sorted(rows, key=lambda w: w.created_at)

    def delete_workspace(self, workspace_id):
        self._delete(PK_WORKSPACE, workspace_id)
        for member in self.list_members(workspace_id):
            self._delete(PK_MEMBER, member.id)

    @staticmethod
    def _member_from_entity(e) -> WorkspaceMember:
        return WorkspaceMember(
            id=e["RowKey"], workspace_id=e.get("workspace_id", ""),
            email=e.get("email", ""), role=WorkspaceRole(e.get("role", "member")),
            display_name=e.get("display_name", ""),
            created_at=_parse_dt(e.get("created_at")) or _now(),
        )

    def add_member(self, member):
        member.email = (member.email or "").strip().lower()
        existing = next((m for m in self.list_members(member.workspace_id)
                         if m.email == member.email), None)
        if existing:
            member.id = existing.id
            member.created_at = existing.created_at
            member.display_name = member.display_name or existing.display_name
        self._upsert(PK_MEMBER, member.id, {
            "workspace_id": member.workspace_id, "email": member.email,
            "role": member.role.value, "display_name": member.display_name,
            "created_at": member.created_at,
        })
        return member

    def remove_member(self, workspace_id, email):
        email = (email or "").strip().lower()
        for member in self.list_members(workspace_id):
            if member.email == email:
                self._delete(PK_MEMBER, member.id)

    def list_members(self, workspace_id):
        rows = [self._member_from_entity(e) for e in self._query(PK_MEMBER)]
        rows = [m for m in rows if m.workspace_id == workspace_id]
        return sorted(rows, key=lambda m: m.created_at)

    def list_memberships(self, email):
        email = (email or "").strip().lower()
        rows = [self._member_from_entity(e) for e in self._query(PK_MEMBER)]
        rows = [m for m in rows if m.email == email]
        return sorted(rows, key=lambda m: m.created_at)

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

    def touch_lock(self, key: str) -> bool:
        if self._get(PK_LOCK, key) is None:
            return False
        self._upsert(PK_LOCK, key, {"acquired_at": _now().isoformat()})
        return True

    def release_lock(self, key: str) -> None:
        self._delete(PK_LOCK, key)
