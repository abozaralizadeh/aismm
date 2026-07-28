"""Local image normalization before publishing.

Real images through Pillow — the failure this prevents is Instagram's
``9004 / 2207052 Media download has failed``, which says nothing about the file
being a WebP, a PNG, too big, or the wrong shape.
"""
import asyncio
import io

import pytest
from PIL import Image

from aismm import media
from aismm.models import Account, Instruction, PlatformName, PublishMode, Run
from aismm.platforms.base import Capabilities
from aismm.platforms.registry import get_platform
from aismm.tools.publish_tool import perform_publish

IG = get_platform(PlatformName.instagram).capabilities


def _image_bytes(fmt="WEBP", size=(1024, 1024), mode="RGB", color=(120, 60, 30)):
    buffer = io.BytesIO()
    Image.new(mode, size, color if mode != "RGBA" else color + (255,)).save(buffer, format=fmt)
    return buffer.getvalue()


def _open(data):
    return Image.open(io.BytesIO(data))


# --- format conversion -------------------------------------------------------------- #

def test_webp_becomes_jpeg():
    """The exact case that failed: a WebP scraped from a web page."""
    out = media.normalize_image(_image_bytes("WEBP"))
    assert _open(out).format == "JPEG"


def test_png_becomes_jpeg():
    """generate_image writes PNG, which Instagram also rejects."""
    out = media.normalize_image(_image_bytes("PNG"))
    assert _open(out).format == "JPEG"


def test_transparency_is_flattened_not_crashed():
    """Pillow raises 'cannot write mode RGBA as JPEG' without this."""
    out = media.normalize_image(_image_bytes("PNG", mode="RGBA"))
    image = _open(out)
    assert image.format == "JPEG" and image.mode == "RGB"


def test_palette_image_is_handled():
    buffer = io.BytesIO()
    Image.new("P", (400, 400)).save(buffer, format="PNG")
    assert _open(media.normalize_image(buffer.getvalue())).format == "JPEG"


# --- aspect ratio -------------------------------------------------------------------- #

def test_portrait_from_the_image_generator_is_padded_into_range():
    """gpt-image-1 portrait is 1024x1536 (0.67) — below Instagram's 0.8 floor."""
    out = media.normalize_image(_image_bytes(size=(1024, 1536)),
                                min_ratio=IG.min_image_ratio, max_ratio=IG.max_image_ratio)
    image = _open(out)
    ratio = image.width / image.height
    assert IG.min_image_ratio <= ratio <= IG.max_image_ratio


def test_very_wide_image_is_padded_into_range():
    out = media.normalize_image(_image_bytes(size=(3000, 500)),
                                min_ratio=IG.min_image_ratio, max_ratio=IG.max_image_ratio,
                                max_width=IG.max_image_width)
    image = _open(out)
    assert IG.min_image_ratio <= image.width / image.height <= IG.max_image_ratio


def test_in_range_image_keeps_its_shape():
    out = media.normalize_image(_image_bytes(size=(1000, 1000)),
                                min_ratio=IG.min_image_ratio, max_ratio=IG.max_image_ratio)
    image = _open(out)
    assert image.width == image.height == 1000


def test_padding_does_not_crop_content():
    """Padding is chosen over cropping so an AI image never loses its subject."""
    out = media.normalize_image(_image_bytes(size=(1000, 1500)),
                                min_ratio=0.8, max_ratio=1.91)
    image = _open(out)
    assert image.width >= 1000 and image.height >= 1500


# --- dimensions and size -------------------------------------------------------------- #

def test_oversized_width_is_reduced():
    out = media.normalize_image(_image_bytes(size=(3000, 2000)), max_width=1440)
    assert _open(out).width == 1440


def test_file_size_cap_is_respected():
    """A noisy image is large; quality/scale must back off until it fits."""
    import os
    noisy = Image.frombytes("RGB", (1200, 1200), os.urandom(1200 * 1200 * 3))
    buffer = io.BytesIO()
    noisy.save(buffer, format="PNG")

    cap = 150_000
    out = media.normalize_image(buffer.getvalue(), max_bytes=cap)
    assert len(out) <= cap


def test_no_constraints_still_returns_jpeg():
    assert _open(media.normalize_image(_image_bytes("WEBP"))).format == "JPEG"


# --- the extension pre-check ----------------------------------------------------------- #

@pytest.mark.parametrize("path,expected", [
    ("/a/b.webp", True), ("/a/b.png", True), ("/a/b.gif", True),
    ("/a/b.jpg", False), ("/a/b.jpeg", False), ("/a/b.JPG", False),
])
def test_needs_conversion_by_extension(path, expected):
    assert media.image_needs_conversion(path, IG) is expected


def test_platform_without_constraints_never_needs_conversion():
    permissive = Capabilities(supports_text=True, supports_image=True, supports_video=True,
                              needs_public_media_url=False, default_orientation="landscape",
                              caption_limit=280)
    assert media.image_needs_conversion("/a/b.webp", permissive) is False


# --- end to end through the publish gate ------------------------------------------------ #

def _publish(store, tmp_path, data, ext, platform=PlatformName.instagram):
    from aismm import assets

    asset = tmp_path / f"source.{ext}"
    asset.write_bytes(data)
    account = store.upsert_account(Account(platform=platform, handle="t", external_id="1"),
                                   access_token="x")
    instruction = store.upsert_instruction(Instruction(name="i", publish_mode=PublishMode.dry_run))
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id))
    state = {"account": account, "instruction": instruction, "store": store, "run": run,
             "assets": []}
    asyncio.run(perform_publish(state, "caption", asset_path=str(asset), media_kind="image"))
    return store.list_staged()[0]


def test_publishing_a_webp_to_instagram_stages_a_jpeg(store, tmp_path, monkeypatch):
    """End to end: the staged asset — what actually gets posted — is a JPEG."""
    from aismm import assets, config as config_module
    import dataclasses

    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))
    staged = _publish(store, tmp_path, _image_bytes("WEBP", size=(1024, 1536)), "webp")

    assert staged.asset_path.endswith(".jpg")
    from pathlib import Path
    image = Image.open(Path(staged.asset_path))
    assert image.format == "JPEG"
    assert IG.min_image_ratio <= image.width / image.height <= IG.max_image_ratio


def test_platform_without_image_rules_leaves_the_asset_alone(store, tmp_path, monkeypatch):
    """X accepts what it's given — don't re-encode for no reason."""
    from aismm import assets, config as config_module
    import dataclasses

    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))
    staged = _publish(store, tmp_path, _image_bytes("PNG"), "png", platform=PlatformName.twitter)
    assert staged.asset_path.endswith(".png")


def test_a_corrupt_image_does_not_block_the_post(store, tmp_path, monkeypatch):
    """Conversion is best-effort: the platform's own error is more useful."""
    from aismm import assets, config as config_module
    import dataclasses

    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))
    staged = _publish(store, tmp_path, b"not-an-image", "webp")
    assert staged.asset_path.endswith(".webp")     # original, unchanged
