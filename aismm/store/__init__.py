"""Persistence layer.

``get_store()`` returns the active :class:`~aismm.store.base.Store`. The default
is the local SQLite implementation; an Azure Table/Blob adapter can be swapped in
without touching callers (see :mod:`aismm.store.azure_store`).
"""
from __future__ import annotations

from functools import lru_cache

from .base import Store
from .local_store import LocalStore


@lru_cache(maxsize=1)
def get_store() -> Store:
    return LocalStore()


__all__ = ["Store", "LocalStore", "get_store"]
