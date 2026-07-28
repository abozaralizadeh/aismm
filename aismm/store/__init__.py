"""Persistence layer.

``get_store()`` returns the active :class:`~aismm.store.base.Store`:

* **local** (default) — SQLite + local asset files. Runs out of the box.
* **azure** — Azure Table Storage for state + Blob storage for media, the way
  the SandBox projects do it. Selected automatically as soon as a storage
  connection string is configured; force either with ``STORE_BACKEND``.

Callers never care which: the interface is identical, and media handling is
routed by :mod:`aismm.assets`.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from ..config import settings
from .base import Store
from .local_store import LocalStore

logger = logging.getLogger("aismm.store")


@lru_cache(maxsize=1)
def get_store() -> Store:
    if settings.use_azure_store:
        from .azure_store import AzureStore

        logger.info("Using Azure Table storage (table=%s, container=%s)",
                    settings.azure_storage.table_name, settings.azure_storage.container_name)
        return AzureStore()
    logger.info("Using local SQLite store (%s)", settings.db_path)
    return LocalStore()


__all__ = ["Store", "LocalStore", "get_store"]
