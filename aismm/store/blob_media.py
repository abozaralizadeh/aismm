"""Azure Blob storage for generated/downloaded media.

Mirrors ``ComicBook/azurestorage.py``'s blob helpers: one container per project,
created on demand, uploads carry an explicit content type, and the blob's own
``url`` is the public address.

This solves the Instagram problem structurally. Instagram FETCHES media from a
public URL rather than accepting an upload, so a locally-served asset needs the
dashboard to be publicly reachable. A blob URL is public on its own — see
``assets.public_url``.

The container must allow anonymous **blob** read for that URL to work
unauthenticated (Azure portal → container → Change access level → "Blob").
Without it uploads still succeed but Instagram cannot fetch the media.

Everything here is lazy: no client is created, and ``azure-storage-blob`` is not
imported, until a connection string is actually configured.
"""
from __future__ import annotations

import logging
from io import BytesIO

from ..config import settings

logger = logging.getLogger("aismm.store.blob")

CONTENT_TYPES = {
    "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "gif": "image/gif",
}

_container_client = None
_ensured = False
_UNASKED = object()
_public_read: object = _UNASKED


def enabled() -> bool:
    return settings.azure_storage.configured


def content_type_for(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return CONTENT_TYPES.get(ext, "application/octet-stream")


def _client():
    """Container client, created once. Raises if storage isn't configured."""
    global _container_client, _ensured
    if _container_client is None:
        from azure.storage.blob import BlobServiceClient

        cfg = settings.azure_storage
        if not cfg.configured:
            raise RuntimeError(
                "Azure Blob storage is not configured — set AZURE_STORAGE_CONNECTION_STRING.")
        service = BlobServiceClient.from_connection_string(cfg.connection_string)
        _container_client = service.get_container_client(cfg.container_name)
    if not _ensured:
        try:
            _container_client.create_container()
            logger.info("Created blob container %s", settings.azure_storage.container_name)
        except Exception:  # noqa: BLE001 - already exists is the normal case
            pass
        _ensured = True
    return _container_client


def reset_client() -> None:
    """Drop the cached client (tests, or a settings change)."""
    global _container_client, _ensured, _public_read
    _container_client, _ensured = None, False
    _public_read = _UNASKED


def upload(name: str, data: bytes, content_type: str | None = None) -> str:
    """Upload bytes under ``name``; return the blob's public URL."""
    from azure.storage.blob import ContentSettings

    blob = _client().get_blob_client(name)
    blob.upload_blob(
        BytesIO(data), overwrite=True,
        content_settings=ContentSettings(content_type=content_type or content_type_for(name)),
    )
    logger.info("Uploaded %s (%d bytes) to blob storage", name, len(data))
    return blob.url


def url(name: str) -> str:
    """Public URL for a blob (no request made)."""
    return _client().get_blob_client(name).url


def public_read() -> bool | None:
    """Whether the container allows anonymous blob reads (``None`` = don't know).

    A blob URL is only usable by a browser — or by Instagram — when the container's
    access level is "Blob". Asked once and cached: the answer changes about as
    often as the storage account does, and the caller is a page render.

    ``None`` is deliberately distinct from ``False``. Being unable to *ask* (no
    permission on the container properties, network blip) is not evidence the
    container is private, and the callers treat the two differently.
    """
    global _public_read
    if _public_read is _UNASKED:
        try:
            level = _client().get_container_properties().public_access
            _public_read = str(level).lower() in {"blob", "container"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the container's access level: %s", exc)
            _public_read = None
    return _public_read


def exists(name: str) -> bool:
    try:
        return _client().get_blob_client(name).exists()
    except Exception:  # noqa: BLE001
        return False


def download(name: str) -> bytes:
    return _client().get_blob_client(name).download_blob().readall()


def delete(name: str) -> None:
    try:
        _client().get_blob_client(name).delete_blob()
    except Exception:  # noqa: BLE001 - absent is fine
        pass
