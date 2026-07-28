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

_JPEG_QUALITIES = (88, 80, 72, 64, 55)


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
        buffer = io.BytesIO()
        # BASELINE, not progressive: Meta's media pipeline is unreliable with
        # progressive JPEGs and reports it only as an opaque processing failure
        # (container status ERROR / code 2207076). Baseline 4:2:0 is the format
        # every platform's decoder handles.
        img.save(buffer, format="JPEG", quality=quality, optimize=True,
                 progressive=False, subsampling="4:2:0")
        return buffer.getvalue()

    data = _jpeg(image, _JPEG_QUALITIES[0])
    if not max_bytes or len(data) <= max_bytes:
        return data

    for quality in _JPEG_QUALITIES[1:]:
        data = _jpeg(image, quality)
        if len(data) <= max_bytes:
            return data

    scaled = image
    for _ in range(4):               # halve area until it fits
        scaled = scaled.resize((max(scaled.width // 2, 1), max(scaled.height // 2, 1)))
        data = _jpeg(scaled, _JPEG_QUALITIES[-1])
        if len(data) <= max_bytes:
            logger.info("Scaled image down to %dx%d to fit %d bytes",
                        scaled.width, scaled.height, max_bytes)
            return data
    logger.warning("Could not get the image under %d bytes (got %d)", max_bytes, len(data))
    return data


def normalize_image(
    data: bytes,
    *,
    max_bytes: int | None = None,
    min_ratio: float | None = None,
    max_ratio: float | None = None,
    max_width: int | None = None,
) -> bytes:
    """Return JPEG bytes that satisfy the given constraints."""
    image = _flatten(_load(data))

    if max_width and image.width > max_width:
        height = max(round(image.height * max_width / image.width), 1)
        image = image.resize((max_width, height))
        logger.info("Resized image to %dx%d (max width %d)", max_width, height, max_width)

    if min_ratio and max_ratio:
        image = _fit_ratio(image, min_ratio, max_ratio)

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
