"""Abstract storage interface.

Any backend (local SQLite, Azure Tables, …) implements this. Tokens are handled
in plaintext at this boundary — implementations encrypt/decrypt internally so the
rest of the app never touches ciphertext or the Fernet key.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from ..models import (
    Account, Instruction, InstructionFile, InstructionState, PlatformApp, Run, RunStatus,
    StagedPost, StagedStatus, Workspace, WorkspaceMember,
)


class Store(ABC):
    # --- schema / lifecycle ------------------------------------------------ #
    @abstractmethod
    def init(self) -> None:
        """Create tables / containers if missing."""

    # --- accounts ---------------------------------------------------------- #
    @abstractmethod
    def upsert_account(
        self,
        account: Account,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
    ) -> Account:
        """Insert/update an account. If plaintext tokens are given, encrypt+store them."""

    @abstractmethod
    def get_account(self, account_id: str) -> Account | None: ...

    @abstractmethod
    def list_accounts(self, *, workspace_id: str | None = None) -> list[Account]:
        """Connected accounts, optionally only those in one workspace."""

    @abstractmethod
    def delete_account(self, account_id: str) -> None: ...

    @abstractmethod
    def get_tokens(self, account_id: str) -> tuple[str, str]:
        """Return decrypted ``(access_token, refresh_token)`` for an account."""

    # --- platform apps (OAuth developer-app credentials) ------------------- #
    @abstractmethod
    def upsert_platform_app(self, app: PlatformApp, *, client_secret: str | None = None) -> PlatformApp:
        """Insert/update an app. A plaintext ``client_secret`` is encrypted here."""

    @abstractmethod
    def get_platform_app(self, app_id: str) -> PlatformApp | None: ...

    @abstractmethod
    def list_platform_apps(self, platform=None) -> list[PlatformApp]: ...

    @abstractmethod
    def delete_platform_app(self, app_id: str) -> None: ...

    @abstractmethod
    def get_app_secret(self, app_id: str) -> str:
        """Return the decrypted client secret for an app."""

    # --- instructions ------------------------------------------------------ #
    @abstractmethod
    def upsert_instruction(self, instruction: Instruction) -> Instruction: ...

    @abstractmethod
    def get_instruction(self, instruction_id: str) -> Instruction | None: ...

    @abstractmethod
    def list_instructions(self, *, enabled_only: bool = False,
                          workspace_id: str | None = None) -> list[Instruction]: ...

    @abstractmethod
    def delete_instruction(self, instruction_id: str) -> None: ...

    # --- instruction attachments ------------------------------------------- #
    @abstractmethod
    def add_instruction_file(self, file: InstructionFile) -> InstructionFile: ...

    @abstractmethod
    def list_instruction_files(self, instruction_id: str) -> list[InstructionFile]: ...

    @abstractmethod
    def get_instruction_file(self, file_id: str) -> InstructionFile | None: ...

    @abstractmethod
    def delete_instruction_file(self, file_id: str) -> None: ...

    # --- instruction state (agent memory + human note) --------------------- #
    @abstractmethod
    def get_state(self, instruction_id: str) -> InstructionState:
        """Return the instruction's carry-over state, creating an empty one if absent."""

    @abstractmethod
    def set_memory(self, instruction_id: str, memory: str, *, compacted: bool = False) -> InstructionState:
        """Replace the agent-written memory. ``compacted`` counts a summarization."""

    @abstractmethod
    def set_note(self, instruction_id: str, note: str) -> InstructionState:
        """Replace the human-written standing note."""

    # --- runs -------------------------------------------------------------- #
    @abstractmethod
    def add_run(self, run: Run) -> Run: ...

    @abstractmethod
    def update_run(self, run: Run) -> Run: ...

    @abstractmethod
    def get_run(self, run_id: str) -> Run | None: ...

    @abstractmethod
    def list_runs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status=None,
        instruction_id: str | None = None,
        account_id: str | None = None,
        workspace_id: str | None = None,
        search: str = "",
        sort: str = "created_at",
        descending: bool = True,
    ) -> list[Run]:
        """A page of runs, filtered and sorted.

        Filtering/sorting/paging belong here rather than in the view: the run
        table grows without bound, and the Azure backend cannot sort or page
        server-side, so each implementation does what its storage allows.
        """

    @abstractmethod
    def count_runs(self, *, status=None, instruction_id: str | None = None,
                   account_id: str | None = None, workspace_id: str | None = None,
                   search: str = "") -> int:
        """How many runs match these filters (for pagination)."""

    def recent_published_runs(self, *, since: datetime | None = None, limit: int = 200,
                              workspace_id: str | None = None) -> list[Run]:
        """Published runs carrying a platform post id, newest first — for metrics.

        The performance feedback loop (:func:`aismm.orchestrator.refresh_metrics`)
        polls each returned run's ``external_id`` for fresh counters. Only runs
        that actually went live carry an id, so runs without one are skipped (older
        posts published before this field existed simply can't be polled). ``since``
        bounds the sweep to recent posts — a months-old post's counts barely move,
        and polling them forever wastes API calls.

        A concrete method rather than ``@abstractmethod``: the default filters
        ``list_runs`` so a backend without a specialised query still works, and
        :class:`LocalStore` overrides it with SQL. Kept next to the run queries so
        both backends stay in step.
        """
        candidates = self.list_runs(status=RunStatus.published, workspace_id=workspace_id,
                                    limit=limit, sort="created_at", descending=True)
        cutoff = _as_utc(since)
        result: list[Run] = []
        for run in candidates:
            if not run.external_id:
                continue
            if cutoff is not None:
                created = _as_utc(run.created_at)
                if created is not None and created < cutoff:
                    continue
            result.append(run)
        return result

    # --- staged posts ------------------------------------------------------ #
    @abstractmethod
    def add_staged(self, staged: StagedPost) -> StagedPost: ...

    @abstractmethod
    def get_staged(self, staged_id: str) -> StagedPost | None: ...

    @abstractmethod
    def update_staged(self, staged: StagedPost) -> StagedPost: ...

    @abstractmethod
    def list_staged(self, *, pending_only: bool = False, limit: int = 100,
                    workspace_id: str | None = None) -> list[StagedPost]: ...

    def open_staged_reply_keys(self, account_id: str) -> set[str]:
        """``{target_type}:{target_id}`` for this account's still-open staged replies.

        "Open" = preview / pending_approval / approved — a reply staged by an
        earlier engagement run that has not been sent or rejected yet. The
        engagement gate (:mod:`aismm.engagement`) uses this to avoid re-staging a
        reply to a comment that is already waiting in the queue.

        A concrete method rather than ``@abstractmethod``: the default scans
        ``list_staged`` so a backend without a specialised query still works, and
        :class:`LocalStore` overrides it with SQL. Kept next to ``list_staged`` so
        both backends stay in step.
        """
        from ..engagement_ledger import key as _key  # local import, avoid cycle

        keys: set[str] = set()
        for staged in self.list_staged(pending_only=False, limit=500):
            if (staged.account_id == account_id and staged.action_type == "reply"
                    and staged.status in (StagedStatus.preview,
                                          StagedStatus.pending_approval,
                                          StagedStatus.approved)):
                keys.add(_key(staged.target_type, staged.target_id))
        return keys

    # --- workspaces -------------------------------------------------------- #
    # A workspace owns accounts, instructions, runs and staged posts. Platform
    # APP credentials are deliberately NOT scoped: they are deployment
    # infrastructure (a Meta app, an X app), the tokens minted from them live on
    # the Account, and .env credentials have always been global.
    @abstractmethod
    def upsert_workspace(self, workspace: Workspace) -> Workspace: ...

    @abstractmethod
    def get_workspace(self, workspace_id: str) -> Workspace | None: ...

    @abstractmethod
    def list_workspaces(self) -> list[Workspace]: ...

    @abstractmethod
    def delete_workspace(self, workspace_id: str) -> None:
        """Remove a workspace and its memberships. Content is NOT cascaded —
        the caller decides, because deleting instructions and runs by accident
        is unrecoverable."""

    @abstractmethod
    def add_member(self, member: WorkspaceMember) -> WorkspaceMember:
        """Add or update a membership (one row per email per workspace)."""

    @abstractmethod
    def remove_member(self, workspace_id: str, email: str) -> None: ...

    @abstractmethod
    def list_members(self, workspace_id: str) -> list[WorkspaceMember]: ...

    @abstractmethod
    def list_memberships(self, email: str) -> list[WorkspaceMember]:
        """Every workspace this identity belongs to."""

    # --- single-flight locks ---------------------------------------------- #
    @abstractmethod
    def acquire_lock(self, key: str, ttl_seconds: int = 3600) -> bool:
        """Atomically acquire ``key``. Returns True if acquired, False if held.

        A lock older than ``ttl_seconds`` is considered stale and reclaimed.
        """

    @abstractmethod
    def touch_lock(self, key: str) -> bool:
        """Re-stamp a held lock so it does not go stale. False if it is gone.

        This is what makes the TTL safe to keep SHORT. A run holds its lock for
        as long as it is alive and renewing; if the process dies mid-run — a
        gunicorn restart while a dashboard "Run now" thread is in flight — nobody
        renews it, and the next run reclaims it in one TTL instead of being told
        "already running" for half an hour by a run that no longer exists.
        """

    @abstractmethod
    def release_lock(self, key: str) -> None: ...


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a datetime to tz-aware UTC so naive/aware values compare safely.

    Run timestamps can come back naive (SQLite drops the tzinfo), while the
    ``since`` cutoff is aware — comparing them directly raises. Treat a naive value
    as UTC, which is what everything in this app is stored as.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
