"""Generated-media asset storage.

Media the agent generates (Sora MP4s, images) or downloads (``save_media``) is
written under the data dir's ``assets/`` folder and referenced by filename.

**With Azure Blob storage configured**, every asset is ALSO uploaded to the blob
container and :func:`public_url` returns the blob URL instead of a dashboard one.
That is the structural fix for Instagram: it fetches media from a public URL
rather than accepting an upload, so without blob storage the dashboard itself has
to be publicly reachable. A blob URL is public on its own (the container needs
anonymous *blob* read).

The local copy is always written and is the primary read path — X, YouTube and
TikTok upload the bytes directly. :func:`read_bytes` falls back to downloading
from blob storage when the local file is missing, so assets survive a second host
or a wiped data dir.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from .config import settings
from .store import blob_media

logger = logging.getLogger("aismm.assets")


def save_bytes(data: bytes, ext: str) -> str:
    """Write bytes to a new asset file and return its absolute path (as str).

    When blob storage is configured the same bytes are uploaded under the file's
    name; a failed upload is logged and the local asset still works.
    """
    settings.assets_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    path = settings.assets_dir / name
    path.write_bytes(data)

    if blob_media.enabled():
        try:
            blob_media.upload(name, data)
        except Exception as exc:  # noqa: BLE001 - never lose a generated asset over this
            logger.warning("Blob upload failed for %s (keeping local copy): %s", name, exc)
    return str(path)


def read_bytes(asset_path: str) -> bytes:
    """Read an asset, falling back to blob storage if the local file is gone."""
    path = Path(asset_path)
    if path.exists():
        return path.read_bytes()
    if blob_media.enabled():
        data = blob_media.download(path.name)
        # Re-materialize locally so repeated reads in one run are cheap.
        try:
            settings.assets_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            pass
        return data
    raise FileNotFoundError(f"Asset not found: {asset_path}")


def exists(asset_path: str | None) -> bool:
    """Is this asset actually available — locally, or in blob storage?

    Asset files outlive the run that made them, but a path the agent remembers
    from a previous run can still be gone (a wiped data dir, a different host, a
    path it only *believed* it had). Publishing a path that resolves to nothing
    produces a confusing platform-side failure, so callers check first.
    """
    if not asset_path:
        return False
    if Path(asset_path).exists():
        return True
    if blob_media.enabled():
        try:
            return blob_media.exists(Path(asset_path).name)
        except Exception:  # noqa: BLE001 - treat an unreachable blob as absent
            return False
    return False


def public_url(asset_path: str | None) -> str:
    """Public URL an asset is reachable at (for Instagram + dashboard previews)."""
    if not asset_path:
        return ""
    name = Path(asset_path).name
    if blob_media.enabled():
        try:
            return blob_media.url(name)
        except Exception as exc:  # noqa: BLE001 - fall back to serving it ourselves
            logger.warning("Could not build a blob URL for %s: %s", name, exc)
    return settings.dashboard.external_url(f"assets/{name}")


def kind_from_path(asset_path: str | None) -> str:
    if not asset_path:
        return "text"
    ext = Path(asset_path).suffix.lower().lstrip(".")
    if ext in {"mp4", "mov", "webm"}:
        return "video"
    if ext in {"png", "jpg", "jpeg", "webp", "gif"}:
        return "image"
    return "text"
