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

**With blob configured, the local folder is therefore a CACHE, not the archive.**
Every asset generated is kept forever otherwise — a Sora clip is tens of MB and a
comic panel a few — and a small VM fills up and then fails at the point where it
tries to write the next one. :func:`prune_local` deletes local files past a
retention window, and it will only delete a file it has just confirmed is in
blob storage: the local copy is disposable, the blob copy is not.
"""
from __future__ import annotations

import logging
import time
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


def browser_url(asset_path: str | None) -> str:
    """Blob URL for a dashboard ``<img>``/``<video>``, or ``""`` to serve it ourselves.

    Pointing the browser straight at storage means the VM no longer reads every
    thumbnail off disk — or, once the file is pruned, pulls it back out of blob
    and re-streams it — on every page render. That is the point of treating the
    local folder as a cache: a pruned file must not become a slower page.

    Returns ``""`` when the blob URL would not work, and the caller falls back to
    ``/assets``:

    * **The container is not anonymously readable** — the blob URL would 401 in
      the browser. ``public_read()`` returning ``None`` means we could not *ask*,
      which is treated as "don't risk it": our own route always works, so an
      unknown is never worth a broken preview.
    * **No blob storage at all** — the local file is all there is.

    Downloads deliberately do not use this: a blob URL cannot set
    ``Content-Disposition: attachment`` without a SAS token, and that header is
    the only way to save a video out of iOS Safari.
    """
    if not asset_path or not blob_media.enabled():
        return ""
    if blob_media.public_read() is not True:
        return ""
    try:
        return blob_media.url(Path(asset_path).name)
    except Exception as exc:  # noqa: BLE001 - serve it ourselves instead
        logger.warning("Could not build a blob URL for %s: %s", asset_path, exc)
        return ""


def kind_from_path(asset_path: str | None) -> str:
    if not asset_path:
        return "text"
    ext = Path(asset_path).suffix.lower().lstrip(".")
    if ext in {"mp4", "mov", "webm"}:
        return "video"
    if ext in {"png", "jpg", "jpeg", "webp", "gif"}:
        return "image"
    return "text"


def local_usage() -> dict:
    """How much disk the local asset cache is using right now."""
    directory = settings.assets_dir
    if not directory.exists():
        return {"files": 0, "bytes": 0, "oldest_days": 0.0}
    files = [p for p in directory.iterdir() if p.is_file()]
    now = time.time()
    oldest = min((now - p.stat().st_mtime for p in files), default=0.0)
    return {"files": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "oldest_days": round(oldest / 86400, 1)}


def prune_local(older_than_days: int = 14, *, apply: bool = False,
                keep: set[str] | None = None) -> dict:
    """Delete cached asset files that blob storage already holds.

    The safety property is simple and absolute: **a file is only deleted after
    ``blob_media.exists`` confirms the blob copy is there.** A local-only asset —
    anything written while blob was unconfigured or while an upload was failing —
    is never touched, whatever its age, because for that file the local disk IS
    the archive.

    Nothing happens at all when blob storage is not configured: without it there
    is no second copy to fall back on, and pruning would simply destroy media.

    ``keep`` is a set of filenames to spare regardless of age — the caller passes
    the assets of recent runs so a preview or a republish does not have to go
    back to blob for something you are still looking at.
    """
    result = {"scanned": 0, "deleted": 0, "freed_bytes": 0, "kept_local_only": 0,
              "skipped_recent": 0, "applied": apply}
    if not blob_media.enabled():
        result["skipped"] = "blob storage is not configured — local files are the only copy"
        return result

    directory = settings.assets_dir
    if not directory.exists():
        return result

    cutoff = time.time() - max(older_than_days, 0) * 86400
    keep = keep or set()
    for path in directory.iterdir():
        if not path.is_file() or path.name in keep:
            continue
        result["scanned"] += 1
        if path.stat().st_mtime > cutoff:
            result["skipped_recent"] += 1
            continue
        try:
            if not blob_media.exists(path.name):
                # The only copy. Leave it, and say so — a growing count here
                # means uploads are failing and the cache is becoming an archive.
                result["kept_local_only"] += 1
                continue
        except Exception as exc:  # noqa: BLE001 - an unreachable blob is not a licence to delete
            logger.warning("Could not confirm %s in blob storage; keeping it: %s", path.name, exc)
            result["kept_local_only"] += 1
            continue

        size = path.stat().st_size
        if apply:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("Could not delete %s: %s", path, exc)
                continue
        result["deleted"] += 1
        result["freed_bytes"] += size

    if result["deleted"]:
        logger.info("%s %d cached asset(s), %.1f MB%s",
                    "Pruned" if apply else "Would prune", result["deleted"],
                    result["freed_bytes"] / 1e6, "" if apply else " (dry run)")
    if result["kept_local_only"]:
        logger.warning("%d old asset(s) are NOT in blob storage and were kept — check that "
                       "uploads are working, or they will fill the disk",
                       result["kept_local_only"])
    return result
