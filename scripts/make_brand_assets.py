"""Render the raster favicons from the same geometry as the SVG mark.

Browsers still want a ``.ico``, and iOS wants a PNG ``apple-touch-icon`` — an SVG
favicon alone leaves both looking wrong. Generating them here rather than
committing opaque binaries means the mark has ONE definition: change the palette
or the glyph below and re-run, instead of hand-editing pixels and hoping the SVG
and the PNG still agree.

Uses Pillow, already a dependency (media.py normalizes images with it), so this
adds nothing to install.

    python scripts/make_brand_assets.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw  # noqa: E402

BRAND = Path(__file__).resolve().parents[1] / "aismm/dashboard/static"
OUT = BRAND / "brand"

# From the design: AISM² — AI Social Media Manager.
INK = "#1c1e27"
PAPER = "#faf9f6"
ACCENT = "#E85C7A"

# The A, in a 0-100 box. Same numbers as the SVGs — see brand/_glyph.txt for why
# this is a path and not a font.
A_OUTER = [(50, 8), (84, 92), (64, 92), (58, 74), (42, 74), (36, 92), (16, 92)]
A_COUNTER = [(50, 38), (55.33, 62), (44.67, 62)]

# Supersample, then downscale: Pillow's polygon fill has no antialiasing.
SCALE = 8


def _draw_icon(size: int, *, circle: bool = False) -> Image.Image:
    box = size * SCALE
    image = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if circle:
        draw.ellipse([0, 0, box - 1, box - 1], fill=INK)
    else:
        draw.rounded_rectangle([0, 0, box - 1, box - 1], radius=box * 0.22, fill=INK)

    # The glyph, placed exactly as in icon.svg: translate(20.2 20.2) scale(0.595).
    def place(points):
        return [((20.2 + x * 0.595) / 100 * box, (20.2 + y * 0.595) / 100 * box)
                for x, y in points]

    draw.polygon(place(A_OUTER), fill=PAPER)
    # The counter is punched by painting the tile colour back over it — the tile
    # underneath is solid, so this is equivalent to a hole and needs no mask.
    draw.polygon(place(A_COUNTER), fill=INK)

    badge = box * 0.25
    inset = box * 0.094
    draw.rounded_rectangle(
        [box - inset - badge, inset, box - inset, inset + badge],
        radius=badge * 0.25, fill=ACCENT, outline=INK, width=max(int(box * 0.024), 1))

    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    # Multi-size .ico: Windows/older browsers pick the size they want.
    icon = _draw_icon(256)
    (BRAND / "favicon.ico").unlink(missing_ok=True)
    icon.save(BRAND / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])

    _draw_icon(32).save(BRAND / "favicon-32.png")
    # iOS composites onto white and applies its own rounding, so it gets the
    # square tile rather than the rounded one being clipped twice.
    _draw_icon(180).save(BRAND / "apple-touch-icon.png")
    _draw_icon(512).save(OUT / "icon-512.png")
    _draw_icon(512, circle=True).save(OUT / "avatar-512.png")

    for path in (BRAND / "favicon.ico", BRAND / "favicon-32.png",
                 BRAND / "apple-touch-icon.png", OUT / "icon-512.png",
                 OUT / "avatar-512.png"):
        print(f"  {path.relative_to(BRAND.parents[2])}  ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
