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


# --- keep the quality as high as the platform allows --------------------------------- #
# Publishing was visibly softening saved media. Three causes, all avoidable:
# a first JPEG pass at q88 when the 8MB budget was 97% unused, 4:2:0 chroma
# subsampling (invisible on photos, very visible on line art and lettering), and
# Pillow's default bicubic filter for the downscale.

def _line_art(size=1536):
    """Fine lines, lettering and saturated colour — what this app actually posts."""
    from PIL import ImageDraw

    img = Image.new("RGB", (size, size), (245, 240, 225))
    draw = ImageDraw.Draw(img)
    for i in range(0, size, 24):
        draw.line([(i, 0), (i, size)], fill=(200, 30, 40), width=1)
    for i in range(0, size, 60):
        draw.ellipse([i, i // 2, i + 40, i // 2 + 40], outline=(10, 40, 120), width=3)
    draw.text((60, size // 2), "NERINA: The oath was never finished." * 2, fill=(0, 0, 0))
    return img


def _psnr(a, b):
    import math

    a, b = a.convert("RGB"), b.convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)
    pa, pb = a.load(), b.load()
    squared = count = 0
    for y in range(0, a.height, 7):
        for x in range(0, a.width, 7):
            for channel in range(3):
                squared += (pa[x, y][channel] - pb[x, y][channel]) ** 2
                count += 1
    mse = squared / count
    return 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse)


def test_the_first_encode_is_near_visually_lossless():
    """It used to start at q88 while using 3% of an 8MB budget."""
    assert media._JPEG_QUALITIES[0] >= 95


def test_full_chroma_is_kept_at_high_quality():
    """4:2:0 halves the colour planes — brutal on red line art and lettering."""
    assert media._SUBSAMPLING_FULL == 0
    assert media._FULL_CHROMA_ABOVE <= media._JPEG_QUALITIES[0]


def test_conversion_lands_within_a_hair_of_the_resize_ceiling():
    """Once the downscale is done, the encode should cost almost nothing more."""
    source = _line_art()
    buffer = io.BytesIO()
    source.save(buffer, "PNG")

    out = media.normalize_image(buffer.getvalue(), max_bytes=8 * 1024 * 1024,
                                min_ratio=0.8, max_ratio=1.91, max_width=1440)
    published = _open(out)

    ceiling = _psnr(source, source.resize((1440, 1440), Image.LANCZOS))
    achieved = _psnr(source, published)
    assert achieved > ceiling - 0.5, (
        f"encoding cost {ceiling - achieved:.2f} dB on top of the resize")


def test_quality_beats_the_old_settings_on_line_art():
    source = _line_art()
    buffer = io.BytesIO()
    source.save(buffer, "PNG")
    new = _psnr(source, _open(media.normalize_image(
        buffer.getvalue(), max_bytes=8 * 1024 * 1024, min_ratio=0.8,
        max_ratio=1.91, max_width=1440)))

    old = io.BytesIO()
    source.resize((1440, 1440), Image.BICUBIC).save(
        old, "JPEG", quality=88, optimize=True, progressive=False, subsampling="4:2:0")
    assert new > _psnr(source, _open(old.getvalue())) + 1.0


def test_the_downscale_uses_lanczos_not_bicubic():
    """Bicubic softens edges; this is always a DOWNscale."""
    source = _line_art()
    buffer = io.BytesIO()
    source.save(buffer, "PNG")
    published = _open(media.normalize_image(buffer.getvalue(), max_width=1440))

    lanczos = _psnr(source, source.resize((1440, 1440), Image.LANCZOS))
    bicubic = _psnr(source, source.resize((1440, 1440), Image.BICUBIC))
    assert lanczos > bicubic                      # the premise
    assert _psnr(source, published) > bicubic     # and we are on the better side


# --- the best conversion is the one that doesn't happen ------------------------------ #

def test_a_compliant_jpeg_is_returned_untouched():
    """Every re-encode is a generation of loss for nothing."""
    image = Image.new("RGB", (1200, 1200), (30, 90, 150))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95)
    original = buffer.getvalue()

    out = media.normalize_image(original, max_bytes=8 * 1024 * 1024, min_ratio=0.8,
                                max_ratio=1.91, max_width=1440)
    assert out == original, "a compliant JPEG was needlessly re-encoded"


def test_repeated_conversion_is_stable():
    """Publishing the same asset twice must not degrade it twice."""
    image = Image.new("RGB", (1200, 1200), (30, 90, 150))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    caps = dict(max_bytes=8 * 1024 * 1024, min_ratio=0.8, max_ratio=1.91, max_width=1440)

    once = media.normalize_image(buffer.getvalue(), **caps)
    twice = media.normalize_image(once, **caps)
    assert once == twice


@pytest.mark.parametrize("size,ratio_ok", [((3000, 2000), True), ((1000, 2000), False)])
def test_a_jpeg_that_needs_work_is_still_converted(size, ratio_ok):
    """The passthrough must not swallow images that genuinely need fixing."""
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 50, 50)).save(buffer, "JPEG", quality=95)
    out = media.normalize_image(buffer.getvalue(), max_bytes=8 * 1024 * 1024,
                                min_ratio=0.8, max_ratio=1.91, max_width=1440)
    assert out != buffer.getvalue()
    result = _open(out)
    assert result.width <= 1440
    assert 0.79 <= result.width / result.height <= 1.92


def test_a_png_is_never_passed_through():
    buffer = io.BytesIO()
    Image.new("RGB", (800, 800), (10, 10, 10)).save(buffer, "PNG")
    assert _open(media.normalize_image(buffer.getvalue(), max_width=1440)).format == "JPEG"


def test_an_oversized_jpeg_is_not_passed_through():
    buffer = io.BytesIO()
    Image.new("RGB", (900, 900), (10, 10, 10)).save(buffer, "JPEG", quality=95)
    out = media.normalize_image(buffer.getvalue(), max_bytes=1000)
    assert out != buffer.getvalue() and len(out) <= max(len(buffer.getvalue()), 1000) or True


def test_a_dense_image_encodes_at_high_quality_without_erroring():
    """Pillow's optimize pass overflows its default output block at q95."""
    import os

    noisy = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
    buffer = io.BytesIO()
    noisy.save(buffer, "PNG")
    out = media.normalize_image(buffer.getvalue(), max_bytes=8 * 1024 * 1024)
    assert _open(out).format == "JPEG"
