"""Abstract storage interface.

Any backend (local SQLite, Azure Tables, …) implements this. Tokens are handled
in plaintext at this boundary — implementations encrypt/decrypt internally so the
rest of the app never touches ciphertext or the Fernet key.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Account, Instruction, InstructionState, PlatformApp, Run, StagedPost


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
    def list_accounts(self) -> list[Account]: ...

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
    def list_instructions(self, *, enabled_only: bool = False) -> list[Instruction]: ...

    @abstractmethod
    def delete_instruction(self, instruction_id: str) -> None: ...

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
                   account_id: str | None = None, search: str = "") -> int:
        """How many runs match these filters (for pagination)."""

    # --- staged posts ------------------------------------------------------ #
    @abstractmethod
    def add_staged(self, staged: StagedPost) -> StagedPost: ...

    @abstractmethod
    def get_staged(self, staged_id: str) -> StagedPost | None: ...

    @abstractmethod
    def update_staged(self, staged: StagedPost) -> StagedPost: ...

    @abstractmethod
    def list_staged(self, *, pending_only: bool = False, limit: int = 100) -> list[StagedPost]: ...

    # --- single-flight locks ---------------------------------------------- #
    @abstractmethod
    def acquire_lock(self, key: str, ttl_seconds: int = 3600) -> bool:
        """Atomically acquire ``key``. Returns True if acquired, False if held.

        A lock older than ``ttl_seconds`` is considered stale and reclaimed.
        """

    @abstractmethod
    def release_lock(self, key: str) -> None: ...
