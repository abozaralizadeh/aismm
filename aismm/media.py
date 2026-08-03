"""Local media normalization — make an asset satisfy a platform's rules.

Platforms are strict about what they will fetch. Instagram accepts **JPEG only**
for images (8 MB max, aspect ratio 4:5–1.91:1, width ≤1440), and answers anything
else with an unhelpful ``Media download has failed`` — the URL is fine, the
*file* is not. A WebP scraped from a web page, or the PNG our image generator
writes, both fail that way.

So instead of hoping the source format is acceptable, we convert locally with
Pillow before publishing. Three things get fixed, in this order:

1. **Format** — anything the platform doesn't accept is re-encoded (JPEG has no
   alpha channel, so RGBA/palette images are flattened onto a background first;
   without that Pillow raises "cannot write mode RGBA as JPEG").
2. **Dimensions** — downscaled past the platform's maximum width, and *padded*
   to the nearest allowed aspect ratio. Padding rather than cropping is
   deliberate: cropping an AI-generated image can cut the subject out, and a
   1024x1536 portrait (0.67) is outside Instagram's 0.8 floor, so this triggers
   routinely. The pad colour is sampled from the image so the bars are unobtrusive.
3. **File size** — JPEG quality steps down, then the image is scaled, until it
   fits the byte cap.

Nothing here touches video: re-encoding one needs ffmpeg, which is not a
dependency. An unsupported video format is reported rather than silently mangled.
"""
from __future__ import annotations

import io
import logging
import math

logger = logging.getLogger("aismm.media")

# Start as close to visually lossless as JPEG gets and only back off if the
# platform's byte cap forces it. The first attempt used to be 88, which threw
# away detail for nothing: a 1440px panel lands around 2-3% of Instagram's 8 MB
# allowance, so there is a lot of headroom to spend on quality.
_JPEG_QUALITIES = (95, 90, 85, 78, 70, 60)

# 4:4:4 — full chroma resolution. 4:2:0 halves the colour planes, which is
# invisible on a photo and very visible on the things this app actually posts:
# line art, lettering, saturated flat colour. Only used once the size cap bites.
_SUBSAMPLING_FULL = 0
_SUBSAMPLING_HALF = "4:2:0"

# Quality at or above this keeps full chroma; below it we are already fighting
# for bytes, so the colour planes go too.
_FULL_CHROMA_ABOVE = 85


def _load(data: bytes):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(data))
    # Honour EXIF rotation now; the tag is dropped when we re-encode.
    return ImageOps.exif_transpose(image)


def _flatten(image, background=(255, 255, 255)):
    """JPEG has no alpha — composite transparency onto a solid background."""
    if image.mode in {"RGBA", "LA", "P"}:
        from PIL import Image

        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, background)
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB")


def _pad_color(image) -> tuple[int, int, int]:
    """A representative colour for padding: the image's average."""
    try:
        return image.resize((1, 1)).convert("RGB").getpixel((0, 0))
    except Exception:  # noqa: BLE001
        return (0, 0, 0)


def _fit_ratio(image, min_ratio: float, max_ratio: float):
    """Pad (never crop) until width/height sits inside the allowed range.

    Targets slightly INSIDE the bounds. Landing exactly on the limit (a 4:5 pad
    computing to 0.79997 after integer rounding) is a coin flip on the
    platform's own comparison, and the rejection tells you nothing.
    """
    from PIL import Image

    width, height = image.size
    if not width or not height:
        return image
    ratio = width / height
    if min_ratio <= ratio <= max_ratio:
        return image

    margin = 1.01                    # 1% inside the boundary
    if ratio < min_ratio:            # too tall -> widen
        new_width, new_height = int(math.ceil(height * min_ratio * margin)), height
    else:                            # too wide -> heighten
        new_width, new_height = width, int(math.ceil(width / max_ratio * margin))

    canvas = Image.new("RGB", (max(new_width, width), max(new_height, height)),
                       _pad_color(image))
    canvas.paste(image, ((canvas.width - width) // 2, (canvas.height - height) // 2))
    logger.info("Padded image %dx%d -> %dx%d to reach an allowed aspect ratio",
                width, height, canvas.width, canvas.height)
    return canvas


def _encode(image, max_bytes: int | None) -> bytes:
    """Encode as JPEG, backing off on quality then size until it fits."""
    def _jpeg(img, quality) -> bytes:
        from PIL import ImageFile

        subsampling = (_SUBSAMPLING_FULL if quality >= _FULL_CHROMA_ABOVE
                       else _SUBSAMPLING_HALF)
        # `optimize` runs the encoder through a single output block, and Pillow's
        # default is far too small for a dense image at high quality — it raises
        # "broken data stream when writing image file" / "Suspension not allowed
        # here". Size the block to the image and it is fine.
        previous = ImageFile.MAXBLOCK
        ImageFile.MAXBLOCK = max(previous, img.width * img.height * 3 + 65536)
        try:
            buffer = io.BytesIO()
            # BASELINE, not progressive: Meta's media pipeline is unreliable with
            # progressive JPEGs and reports it only as an opaque processing
            # failure (container status ERROR / code 2207076). Baseline is the
            # format every platform's decoder handles.
            img.save(buffer, format="JPEG", quality=quality, optimize=True,
                     progressive=False, subsampling=subsampling)
            return buffer.getvalue()
        except OSError:
            # Still unhappy (an enormous image): drop `optimize`, which only buys
            # a few percent of size and is the part that needs the big block.
            logger.info("JPEG optimize pass failed at q%d; encoding without it", quality)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality, progressive=False,
                     subsampling=subsampling)
            return buffer.getvalue()
        finally:
            ImageFile.MAXBLOCK = previous

    data = _jpeg(image, _JPEG_QUALITIES[0])
    if not max_bytes or len(data) <= max_bytes:
        return data

    for quality in _JPEG_QUALITIES[1:]:
        data = _jpeg(image, quality)
        if len(data) <= max_bytes:
            return data

    from PIL import Image

    scaled = image
    for _ in range(4):               # halve area until it fits
        scaled = scaled.resize((max(scaled.width // 2, 1), max(scaled.height // 2, 1)),
                               Image.LANCZOS)
        data = _jpeg(scaled, _JPEG_QUALITIES[-1])
        if len(data) <= max_bytes:
            logger.info("Scaled image down to %dx%d to fit %d bytes",
                        scaled.width, scaled.height, max_bytes)
            return data
    logger.warning("Could not get the image under %d bytes (got %d)", max_bytes, len(data))
    return data


def _already_compliant(data: bytes, *, max_bytes, min_ratio, max_ratio, max_width) -> bool:
    """Is this file already publishable exactly as it is?

    Re-encoding a JPEG that needs nothing done to it costs a generation of
    quality for no benefit, so check first. Deliberately conservative: anything
    unreadable, non-JPEG, animated or with an alpha channel returns False and
    goes through the normal path.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if (image.format or "").upper() != "JPEG":
                return False           # PNG/WebP must be converted regardless
            if getattr(image, "n_frames", 1) > 1:
                return False
            width, height = image.size
            if not (width and height):
                return False
            if max_bytes and len(data) > max_bytes:
                return False
            if max_width and width > max_width:
                return False
            ratio = width / height
            if min_ratio and ratio < min_ratio:
                return False
            if max_ratio and ratio > max_ratio:
                return False
            # EXIF orientation is applied by _load(); if one is set the pixels on
            # disk are not what we would publish, so it is not a no-op.
            exif = image.getexif()
            if exif and exif.get(0x0112, 1) not in (0, 1):
                return False
        return True
    except Exception:  # noqa: BLE001 - unreadable here means "convert it"
        return False


def normalize_image(
    data: bytes,
    *,
    max_bytes: int | None = None,
    min_ratio: float | None = None,
    max_ratio: float | None = None,
    max_width: int | None = None,
) -> bytes:
    """Return JPEG bytes that satisfy the given constraints.

    Returns the input UNTOUCHED when it already satisfies all of them. Every
    re-encode of a JPEG is generation loss, so the best conversion is the one
    that doesn't happen.
    """
    if _already_compliant(data, max_bytes=max_bytes, min_ratio=min_ratio,
                          max_ratio=max_ratio, max_width=max_width):
        logger.info("Image already meets this platform's limits (%d bytes) — "
                    "publishing it as-is", len(data))
        return data

    image = _flatten(_load(data))

    # Ratio FIRST, width second. Padding widens the image, so clamping the width
    # before padding let a tall 1000x2000 come out 1616 wide — over the very
    # limit we had just applied. Doing it in this order means the final width is
    # always the one the platform asked for, and it is still a single resample.
    if min_ratio and max_ratio:
        image = _fit_ratio(image, min_ratio, max_ratio)

    if max_width and image.width > max_width:
        from PIL import Image

        height = max(round(image.height * max_width / image.width), 1)
        # LANCZOS, not Pillow's default bicubic: this is a DOWNscale, where
        # bicubic visibly softens edges and lettering. It costs a little CPU once
        # per post and is the difference between crisp and mushy line art.
        image = image.resize((max_width, height), Image.LANCZOS)
        logger.info("Resized image to %dx%d (max width %d)", max_width, height, max_width)

    return _encode(image, max_bytes)


def image_needs_conversion(asset_path: str, caps) -> bool:
    """Cheap pre-check: is this file's extension one the platform accepts?

    A ``True`` here only means "re-encode it"; the conversion itself also fixes
    size and aspect ratio, which the extension can't tell us about.
    """
    from pathlib import Path

    formats = getattr(caps, "image_formats", None)
    if not formats:
        return False
    ext = Path(asset_path).suffix.lower().lstrip(".")
    return ext not in formats
