"""Local SQLite store (SQLModel). Default backend — runs out of the box.

OAuth tokens are Fernet-encrypted before they touch the DB; ``get_tokens``
returns them decrypted. The ``Lock`` table gives us the same single-flight
guarantee SandBox gets from Azure Tables: an atomic ``INSERT`` on the key is the
lock acquisition, and a row older than the TTL is reclaimed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete
from sqlalchemy import func as sa_func
from sqlalchemy import or_ as sa_or
from sqlmodel import Session, SQLModel, create_engine, select

from ..config import ensure_dirs, settings
from ..crypto import decrypt, encrypt
from ..models import (
    Account, Instruction, InstructionFile, InstructionState, Lock, PlatformApp, Run,
    RunStatus, StagedPost, StagedStatus, Workspace, WorkspaceMember,
)
from .base import Store


logger = logging.getLogger("aismm.store.local")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ws_clause(column, workspace_id):
    """A filter for ``workspace_id``, which may be one id or several.

    Several is how the default workspace also claims rows that carry no
    workspace at all: anything written before workspaces existed, or by a code
    path that forgot to set one. Matching them at READ time keeps the migration
    self-healing without a table scan on every request — and, unlike scanning,
    it cannot lose data by being skipped.
    """
    ids = [workspace_id] if isinstance(workspace_id, str) else list(workspace_id)
    return column.in_(ids) if len(ids) != 1 else column == ids[0]


class LocalStore(Store):
    def __init__(self, db_url: str | None = None) -> None:
        ensure_dirs()
        self._engine = create_engine(
            db_url or settings.db_url,
            connect_args={"check_same_thread": False},
        )
        self.init()

    def init(self) -> None:
        SQLModel.metadata.create_all(self._engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Add columns the models declare but an existing database lacks.

        ``create_all`` creates missing *tables* and nothing else, so widening a
        model used to break every deployment that already had the table — which
        is why per-instruction state was pushed into a side table. This closes
        that gap: SQLite's ``ALTER TABLE ADD COLUMN`` is cheap and non-locking,
        and Azure Table Storage needs no equivalent (it is schemaless).
        """
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text as sa_text

        inspector = sa_inspect(self._engine)
        existing_tables = set(inspector.get_table_names())
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" ' \
                      f'{column.type.compile(self._engine.dialect)}'
                default = column.default.arg if column.default is not None else None
                if isinstance(default, (str, int, float, bool)):
                    literal = f"'{default}'" if isinstance(default, str) else int(default) \
                        if isinstance(default, bool) else default
                    ddl += f" DEFAULT {literal}"
                with self._engine.begin() as conn:
                    conn.execute(sa_text(ddl))
                logger.info("Added missing column %s.%s", table.name, column.name)

    # --- accounts ---------------------------------------------------------- #
    def upsert_account(self, account, *, access_token=None, refresh_token=None):
        with Session(self._engine) as s:
            existing = s.get(Account, account.id)
            if access_token is not None:
                account.access_token_enc = encrypt(access_token)
            elif existing is not None:
                account.access_token_enc = account.access_token_enc or existing.access_token_enc
            if refresh_token is not None:
                account.refresh_token_enc = encrypt(refresh_token)
            elif existing is not None:
                account.refresh_token_enc = account.refresh_token_enc or existing.refresh_token_enc
            merged = s.merge(account)
            s.commit()
            s.refresh(merged)
            return merged

    def get_account(self, account_id):
        with Session(self._engine) as s:
            return s.get(Account, account_id)

    def list_accounts(self, *, workspace_id=None):
        with Session(self._engine) as s:
            stmt = select(Account)
            if workspace_id is not None:
                stmt = stmt.where(_ws_clause(Account.workspace_id, workspace_id))
            return list(s.exec(stmt.order_by(Account.created_at)).all())

    def delete_account(self, account_id):
        with Session(self._engine) as s:
            obj = s.get(Account, account_id)
            if obj:
                s.delete(obj)
                s.commit()

    def get_tokens(self, account_id):
        acct = self.get_account(account_id)
        if not acct:
            return "", ""
        return decrypt(acct.access_token_enc), decrypt(acct.refresh_token_enc)

    # --- platform apps ------------------------------------------------------ #
    def upsert_platform_app(self, app, *, client_secret=None):
        with Session(self._engine) as s:
            existing = s.get(PlatformApp, app.id)
            if client_secret:
                app.client_secret_enc = encrypt(client_secret)
            elif existing is not None:
                app.client_secret_enc = app.client_secret_enc or existing.client_secret_enc
            merged = s.merge(app)
            s.commit()
            s.refresh(merged)
            return merged

    def get_platform_app(self, app_id):
        with Session(self._engine) as s:
            return s.get(PlatformApp, app_id)

    def list_platform_apps(self, platform=None):
        with Session(self._engine) as s:
            stmt = select(PlatformApp)
            if platform is not None:
                stmt = stmt.where(PlatformApp.platform == platform)
            return list(s.exec(stmt.order_by(PlatformApp.created_at)).all())

    def delete_platform_app(self, app_id):
        with Session(self._engine) as s:
            obj = s.get(PlatformApp, app_id)
            if obj:
                s.delete(obj)
                s.commit()

    def get_app_secret(self, app_id):
        app = self.get_platform_app(app_id)
        return decrypt(app.client_secret_enc) if app else ""

    # --- instructions ------------------------------------------------------ #
    def upsert_instruction(self, instruction):
        instruction.updated_at = _now()
        with Session(self._engine) as s:
            merged = s.merge(instruction)
            s.commit()
            s.refresh(merged)
            return merged

    def get_instruction(self, instruction_id):
        with Session(self._engine) as s:
            return s.get(Instruction, instruction_id)

    def list_instructions(self, *, enabled_only=False, workspace_id=None):
        with Session(self._engine) as s:
            stmt = select(Instruction)
            if workspace_id is not None:
                stmt = stmt.where(_ws_clause(Instruction.workspace_id, workspace_id))
            if enabled_only:
                stmt = stmt.where(Instruction.enabled == True)  # noqa: E712
            return list(s.exec(stmt.order_by(Instruction.created_at)).all())

    def delete_instruction(self, instruction_id):
        with Session(self._engine) as s:
            obj = s.get(Instruction, instruction_id)
            if obj:
                s.delete(obj)
            state = s.get(InstructionState, instruction_id)
            if state:
                s.delete(state)          # don't orphan memory/notes
            for attachment in s.exec(select(InstructionFile).where(
                    InstructionFile.instruction_id == instruction_id)).all():
                s.delete(attachment)
            s.commit()

    # --- instruction attachments ------------------------------------------- #
    def add_instruction_file(self, file):
        with Session(self._engine) as s:
            merged = s.merge(file)
            s.commit()
            s.refresh(merged)
            return merged

    def list_instruction_files(self, instruction_id):
        with Session(self._engine) as s:
            stmt = select(InstructionFile).where(
                InstructionFile.instruction_id == instruction_id)
            return list(s.exec(stmt.order_by(InstructionFile.created_at)).all())

    def get_instruction_file(self, file_id):
        with Session(self._engine) as s:
            return s.get(InstructionFile, file_id)

    def delete_instruction_file(self, file_id):
        with Session(self._engine) as s:
            obj = s.get(InstructionFile, file_id)
            if obj:
                s.delete(obj)
                s.commit()

    # --- instruction state (agent memory + human note) --------------------- #
    def get_state(self, instruction_id):
        with Session(self._engine) as s:
            state = s.get(InstructionState, instruction_id)
            return state or InstructionState(instruction_id=instruction_id)

    def _update_state(self, instruction_id, **fields):
        with Session(self._engine) as s:
            state = s.get(InstructionState, instruction_id) or InstructionState(
                instruction_id=instruction_id)
            for key, value in fields.items():
                setattr(state, key, value)
            merged = s.merge(state)
            s.commit()
            s.refresh(merged)
            return merged

    def set_memory(self, instruction_id, memory, *, compacted=False):
        current = self.get_state(instruction_id)
        return self._update_state(
            instruction_id,
            memory=memory or "",
            memory_updated_at=_now(),
            compactions=current.compactions + (1 if compacted else 0),
        )

    def set_note(self, instruction_id, note):
        return self._update_state(instruction_id, note=note or "", note_updated_at=_now())

    # --- runs -------------------------------------------------------------- #
    def add_run(self, run):
        with Session(self._engine) as s:
            s.add(run)
            s.commit()
            s.refresh(run)
            return run

    def update_run(self, run):
        with Session(self._engine) as s:
            merged = s.merge(run)
            s.commit()
            s.refresh(merged)
            return merged

    def get_run(self, run_id):
        with Session(self._engine) as s:
            return s.get(Run, run_id)

    # Sortable columns, whitelisted so a query parameter can't reach arbitrary
    # attributes.
    _RUN_SORTS = {"created_at": Run.created_at, "status": Run.status,
                  "instruction_id": Run.instruction_id, "account_id": Run.account_id}

    def _run_filters(self, session, *, status, instruction_id, account_id, search,
                     workspace_id=None):
        clauses = []
        if workspace_id is not None:
            clauses.append(_ws_clause(Run.workspace_id, workspace_id))
        if status:
            clauses.append(Run.status == status)
        if instruction_id:
            clauses.append(Run.instruction_id == instruction_id)
        if account_id:
            clauses.append(Run.account_id == account_id)
        term = (search or "").strip()
        if term:
            like = f"%{term}%"
            # Also match the instruction's NAME, which is what a human searches
            # for — the run row only stores its id.
            named = session.exec(
                select(Instruction.id).where(Instruction.name.ilike(like))).all()
            text_match = [Run.caption.ilike(like), Run.error.ilike(like),
                          Run.log.ilike(like), Run.external_url.ilike(like)]
            if named:
                text_match.append(Run.instruction_id.in_(list(named)))
            clauses.append(sa_or(*text_match))
        return clauses

    def list_runs(self, *, limit=100, offset=0, status=None, instruction_id=None,
                  account_id=None, workspace_id=None, search="", sort="created_at",
                  descending=True):
        with Session(self._engine) as s:
            column = self._RUN_SORTS.get(sort, Run.created_at)
            stmt = select(Run).where(*self._run_filters(
                s, status=status, instruction_id=instruction_id,
                account_id=account_id, workspace_id=workspace_id, search=search))
            stmt = stmt.order_by(column.desc() if descending else column.asc())
            # A stable tiebreaker keeps paging consistent when sorting by a
            # column with many equal values (status, instruction).
            if sort != "created_at":
                stmt = stmt.order_by(Run.created_at.desc())
            return list(s.exec(stmt.offset(offset).limit(limit)).all())

    def count_runs(self, *, status=None, instruction_id=None, account_id=None,
                   workspace_id=None, search=""):
        with Session(self._engine) as s:
            stmt = select(sa_func.count()).select_from(Run).where(*self._run_filters(
                s, status=status, instruction_id=instruction_id,
                account_id=account_id, workspace_id=workspace_id, search=search))
            return int(s.exec(stmt).one())

    def recent_published_runs(self, *, since=None, limit=200, workspace_id=None):
        """SQL variant of the base scan — published runs with a post id to poll.

        Filters ``external_id != ''`` and ``created_at >= since`` in SQL so the
        limit counts only pollable rows, rather than the base method's post-filter
        which could return fewer than ``limit`` after dropping id-less rows.
        """
        with Session(self._engine) as s:
            stmt = select(Run).where(
                Run.status == RunStatus.published, Run.external_id != "")
            if workspace_id is not None:
                stmt = stmt.where(_ws_clause(Run.workspace_id, workspace_id))
            if since is not None:
                stmt = stmt.where(Run.created_at >= since)
            stmt = stmt.order_by(Run.created_at.desc()).limit(limit)
            return list(s.exec(stmt).all())

    # --- staged posts ------------------------------------------------------ #
    def add_staged(self, staged):
        with Session(self._engine) as s:
            s.add(staged)
            s.commit()
            s.refresh(staged)
            return staged

    def get_staged(self, staged_id):
        with Session(self._engine) as s:
            return s.get(StagedPost, staged_id)

    def update_staged(self, staged):
        with Session(self._engine) as s:
            merged = s.merge(staged)
            s.commit()
            s.refresh(merged)
            return merged

    def list_staged(self, *, pending_only=False, limit=100, workspace_id=None):
        with Session(self._engine) as s:
            stmt = select(StagedPost)
            if workspace_id is not None:
                stmt = stmt.where(_ws_clause(StagedPost.workspace_id, workspace_id))
            if pending_only:
                stmt = stmt.where(StagedPost.status == StagedStatus.pending_approval)
            return list(s.exec(stmt.order_by(StagedPost.created_at.desc()).limit(limit)).all())

    def open_staged_reply_keys(self, account_id):
        """SQL variant of the base scan — the engagement queue-dedup lookup."""
        from ..engagement_ledger import key as _key

        open_states = (StagedStatus.preview, StagedStatus.pending_approval,
                       StagedStatus.approved)
        with Session(self._engine) as s:
            rows = s.exec(
                select(StagedPost.target_type, StagedPost.target_id)
                .where(StagedPost.account_id == account_id)
                .where(StagedPost.action_type == "reply")
                .where(StagedPost.status.in_(open_states))).all()
        return {_key(t_type, t_id) for t_type, t_id in rows}

    # --- workspaces -------------------------------------------------------- #
    def upsert_workspace(self, workspace):
        with Session(self._engine) as s:
            existing = s.get(Workspace, workspace.id)
            if existing:
                for field in ("name", "claims_unassigned", "created_by"):
                    setattr(existing, field, getattr(workspace, field))
                s.add(existing)
                s.commit()
                s.refresh(existing)
                return existing
            s.add(workspace)
            s.commit()
            s.refresh(workspace)
            return workspace

    def get_workspace(self, workspace_id):
        with Session(self._engine) as s:
            return s.get(Workspace, workspace_id)

    def list_workspaces(self):
        with Session(self._engine) as s:
            return list(s.exec(select(Workspace).order_by(Workspace.created_at)).all())

    def delete_workspace(self, workspace_id):
        with Session(self._engine) as s:
            obj = s.get(Workspace, workspace_id)
            if obj:
                s.delete(obj)
            s.exec(sa_delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id))
            s.commit()

    def add_member(self, member):
        email = (member.email or "").strip().lower()
        with Session(self._engine) as s:
            existing = s.exec(select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == member.workspace_id,
                WorkspaceMember.email == email)).first()
            if existing:
                existing.role = member.role
                existing.display_name = member.display_name or existing.display_name
                s.add(existing)
                s.commit()
                s.refresh(existing)
                return existing
            member.email = email
            s.add(member)
            s.commit()
            s.refresh(member)
            return member

    def remove_member(self, workspace_id, email):
        with Session(self._engine) as s:
            s.exec(sa_delete(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.email == (email or "").strip().lower()))
            s.commit()

    def list_members(self, workspace_id):
        with Session(self._engine) as s:
            return list(s.exec(select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id
            ).order_by(WorkspaceMember.created_at)).all())

    def list_memberships(self, email):
        with Session(self._engine) as s:
            return list(s.exec(select(WorkspaceMember).where(
                WorkspaceMember.email == (email or "").strip().lower()
            ).order_by(WorkspaceMember.created_at)).all())

    # --- single-flight locks ---------------------------------------------- #
    def acquire_lock(self, key, ttl_seconds=3600):
        cutoff = _now() - timedelta(seconds=ttl_seconds)
        with Session(self._engine) as s:
            existing = s.get(Lock, key)
            if existing is not None:
                acquired = existing.acquired_at
                if acquired.tzinfo is None:
                    acquired = acquired.replace(tzinfo=timezone.utc)
                if acquired > cutoff:
                    return False  # held and fresh
                # stale — reclaim
                existing.acquired_at = _now()
                s.add(existing)
                s.commit()
                return True
            s.add(Lock(key=key, acquired_at=_now()))
            s.commit()
            return True

    def touch_lock(self, key):
        with Session(self._engine) as s:
            existing = s.get(Lock, key)
            if existing is None:
                return False
            existing.acquired_at = _now()
            s.add(existing)
            s.commit()
            return True

    def release_lock(self, key):
        with Session(self._engine) as s:
            s.exec(sa_delete(Lock).where(Lock.key == key))
            s.commit()
