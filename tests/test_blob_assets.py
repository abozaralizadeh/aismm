"""Asset handling with and without Azure Blob storage.

The important behaviour: with blob storage configured, ``public_url`` returns a
blob URL — which is what makes Instagram publishing work without exposing the
dashboard — and a missing local file falls back to the blob.
"""
import dataclasses

import pytest

from aismm import assets
from aismm import config as config_module
from aismm.config import AzureStorageSettings
from aismm.store import blob_media


@pytest.fixture()
def local_only(monkeypatch, tmp_path):
    patched = dataclasses.replace(config_module.settings, data_dir=tmp_path)
    monkeypatch.setattr(assets, "settings", patched)
    monkeypatch.setattr(blob_media, "enabled", lambda: False)
    return patched


@pytest.fixture()
def with_blob(monkeypatch, tmp_path):
    """Blob storage 'configured', backed by an in-memory container."""
    patched = dataclasses.replace(
        config_module.settings,
        data_dir=tmp_path,
        azure_storage=AzureStorageSettings(connection_string="UseDevelopmentStorage=true"))
    monkeypatch.setattr(assets, "settings", patched)
    monkeypatch.setattr(blob_media, "settings", patched)

    uploaded: dict[str, bytes] = {}

    def fake_upload(name, data, content_type=None):
        uploaded[name] = data
        return f"https://acct.blob.core.windows.net/aismm-media/{name}"

    monkeypatch.setattr(blob_media, "enabled", lambda: True)
    monkeypatch.setattr(blob_media, "upload", fake_upload)
    monkeypatch.setattr(blob_media, "url",
                        lambda name: f"https://acct.blob.core.windows.net/aismm-media/{name}")
    monkeypatch.setattr(blob_media, "download", lambda name: uploaded[name])
    return uploaded


# --- local behaviour is unchanged -------------------------------------------------- #

def test_local_save_and_read(local_only):
    path = assets.save_bytes(b"data", "mp4")
    assert path.endswith(".mp4")
    assert assets.read_bytes(path) == b"data"


def test_local_public_url_points_at_the_dashboard(local_only):
    path = assets.save_bytes(b"data", "png")
    assert assets.public_url(path).startswith(local_only.dashboard.public_base_url)
    assert "/assets/" in assets.public_url(path)


def test_missing_asset_without_blob_raises(local_only):
    with pytest.raises(FileNotFoundError):
        assets.read_bytes(str(local_only.assets_dir / "gone.mp4"))


# --- with blob storage --------------------------------------------------------------- #

def test_asset_is_mirrored_to_blob(with_blob):
    path = assets.save_bytes(b"video-bytes", "mp4")
    name = path.rsplit("/", 1)[-1]
    assert with_blob[name] == b"video-bytes"


def test_public_url_becomes_a_blob_url(with_blob):
    """This is what lets Instagram fetch media without a public dashboard."""
    path = assets.save_bytes(b"x", "jpg")
    url = assets.public_url(path)
    assert url.startswith("https://acct.blob.core.windows.net/aismm-media/")
    assert url.endswith(".jpg")


def test_local_copy_is_still_written(with_blob):
    """X/YouTube/TikTok upload bytes directly, so the local file stays primary."""
    import pathlib
    path = assets.save_bytes(b"x", "mp4")
    assert pathlib.Path(path).exists()


def test_read_falls_back_to_blob_when_the_local_file_is_gone(with_blob):
    import pathlib
    path = assets.save_bytes(b"original", "mp4")
    pathlib.Path(path).unlink()
    assert assets.read_bytes(path) == b"original"
    assert pathlib.Path(path).exists()      # re-materialized locally


def test_a_failed_upload_does_not_lose_the_asset(with_blob, monkeypatch):
    def boom(name, data, content_type=None):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(blob_media, "upload", boom)
    path = assets.save_bytes(b"still-here", "mp4")
    assert assets.read_bytes(path) == b"still-here"


def test_public_url_falls_back_when_blob_url_fails(with_blob, monkeypatch):
    monkeypatch.setattr(blob_media, "url",
                        lambda name: (_ for _ in ()).throw(RuntimeError("no client")))
    path = assets.save_bytes(b"x", "png")
    assert "/assets/" in assets.public_url(path)


def test_empty_path_has_no_url(with_blob):
    assert assets.public_url("") == ""


# --- content types ------------------------------------------------------------------- #

@pytest.mark.parametrize("name,expected", [
    ("a.mp4", "video/mp4"), ("a.png", "image/png"), ("a.jpg", "image/jpeg"),
    ("a.webp", "image/webp"), ("a.bin", "application/octet-stream"),
])
def test_blob_content_type_mapping(name, expected):
    assert blob_media.content_type_for(name) == expected
