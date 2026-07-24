"""Abstract storage interface.

Any backend (local SQLite, Azure Tables, …) implements this. Tokens are handled
in plaintext at this boundary — implementations encrypt/decrypt internally so the
rest of the app never touches ciphertext or the Fernet key.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Account, Instruction, Run, StagedPost


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

    # --- instructions ------------------------------------------------------ #
    @abstractmethod
    def upsert_instruction(self, instruction: Instruction) -> Instruction: ...

    @abstractmethod
    def get_instruction(self, instruction_id: str) -> Instruction | None: ...

    @abstractmethod
    def list_instructions(self, *, enabled_only: bool = False) -> list[Instruction]: ...

    @abstractmethod
    def delete_instruction(self, instruction_id: str) -> None: ...

    # --- runs -------------------------------------------------------------- #
    @abstractmethod
    def add_run(self, run: Run) -> Run: ...

    @abstractmethod
    def update_run(self, run: Run) -> Run: ...

    @abstractmethod
    def list_runs(self, *, limit: int = 100) -> list[Run]: ...

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
