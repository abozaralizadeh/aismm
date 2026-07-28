"""Local SQLite store (SQLModel). Default backend — runs out of the box.

OAuth tokens are Fernet-encrypted before they touch the DB; ``get_tokens``
returns them decrypted. The ``Lock`` table gives us the same single-flight
guarantee SandBox gets from Azure Tables: an atomic ``INSERT`` on the key is the
lock acquisition, and a row older than the TTL is reclaimed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete
from sqlmodel import Session, SQLModel, create_engine, select

from ..config import ensure_dirs, settings
from ..crypto import decrypt, encrypt
from ..models import (
    Account, Instruction, InstructionState, Lock, Run, StagedPost, StagedStatus,
)
from .base import Store


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

    def list_accounts(self):
        with Session(self._engine) as s:
            return list(s.exec(select(Account).order_by(Account.created_at)).all())

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

    def list_instructions(self, *, enabled_only=False):
        with Session(self._engine) as s:
            stmt = select(Instruction)
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

    def list_runs(self, *, limit=100):
        with Session(self._engine) as s:
            return list(
                s.exec(select(Run).order_by(Run.created_at.desc()).limit(limit)).all()
            )

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

    def list_staged(self, *, pending_only=False, limit=100):
        with Session(self._engine) as s:
            stmt = select(StagedPost)
            if pending_only:
                stmt = stmt.where(StagedPost.status == StagedStatus.pending_approval)
            return list(s.exec(stmt.order_by(StagedPost.created_at.desc()).limit(limit)).all())

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

    def release_lock(self, key):
        with Session(self._engine) as s:
            s.exec(sa_delete(Lock).where(Lock.key == key))
            s.commit()
