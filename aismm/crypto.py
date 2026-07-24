"""Symmetric encryption for OAuth tokens at rest (Fernet).

The key is resolved once, in this order:
1. ``AISMM_TOKEN_KEY`` env var (a urlsafe base64 Fernet key), or
2. a ``tokens.key`` file next to the data dir (generated on first use).

Callers work in plaintext; the store persists only the encrypted form.
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.token_key.strip()
    if not key:
        key_file = settings.data_dir / "tokens.key"
        if key_file.exists():
            key = key_file.read_text().strip()
        else:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key().decode()
            key_file.write_text(key)
            try:
                key_file.chmod(0o600)
            except OSError:
                pass
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str | None) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str | None) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        return ""
