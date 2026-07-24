"""Generated-media asset storage.

Media the agent generates (Sora MP4s, images) is written under the data dir's
``assets/`` folder and referenced by filename. The dashboard serves these at
``<DASHBOARD_BASE_URL>/assets/<filename>`` — which is also the PUBLIC url
Instagram needs to fetch media (see the Instagram platform + README).
"""
from __future__ import annotations

import uuid
from pathlib import Path

from .config import settings


def save_bytes(data: bytes, ext: str) -> str:
    """Write bytes to a new asset file and return its absolute path (as str)."""
    settings.assets_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
    path = settings.assets_dir / name
    path.write_bytes(data)
    return str(path)


def public_url(asset_path: str | None) -> str:
    """Public URL the dashboard serves an asset at (for Instagram + previews)."""
    if not asset_path:
        return ""
    name = Path(asset_path).name
    return f"{settings.dashboard.base_url.rstrip('/')}/assets/{name}"


def kind_from_path(asset_path: str | None) -> str:
    if not asset_path:
        return "text"
    ext = Path(asset_path).suffix.lower().lstrip(".")
    if ext in {"mp4", "mov", "webm"}:
        return "video"
    if ext in {"png", "jpg", "jpeg", "webp", "gif"}:
        return "image"
    return "text"
