"""The platform brand marks, inlined into the dashboard.

"3 accounts" does not say *where* it posts, and that is the thing an operator
scanning the instruction list actually wants to know. A small Instagram and X
glyph beside the count says it at a glance, in less room than the words.

The marks come from Simple Icons (CC0) and live as SVG files in
``static/brand/platforms/`` — see the NOTICE there. This module reads the path
out of those files ONCE at import, so the file is the single definition and
there is no second copy in Python to drift from it.

They are inlined rather than served as ``<img>`` for two reasons: a table row
would otherwise fire four extra requests, and an ``<img>`` cannot be recoloured.
X and TikTok publish monochrome marks, so they take ``currentColor`` and work in
either theme; Instagram and YouTube are only themselves in their own colour.

An unknown platform yields nothing at all. A missing icon must never be the
reason a page 500s — the platform's *name* is still in the DOM for a11y.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from markupsafe import Markup

logger = logging.getLogger("aismm.dashboard.icons")

ICON_DIR = Path(__file__).resolve().parent / "static/brand/platforms"

# Brand colour, or "" to inherit the surrounding text colour. X's and TikTok's
# marks ARE monochrome by design, so inheriting is correct rather than a
# compromise — and it is what keeps them visible on the dark dashboard.
# Keyed by PlatformName VALUE, so `twitter` — the enum member kept its original
# name. The mark and its file are called `x`, because that is the brand now; the
# two are bridged here rather than by renaming an enum every row in the DB uses.
COLORS = {"instagram": "#E4405F", "youtube": "#FF0000", "twitter": "", "tiktok": "",
          "linkedin": "#0A66C2", "facebook": "#0866FF"}
FILES = {"twitter": "x"}

_PATH = re.compile(r'\sd="([^"]+)"')

_paths: dict[str, str] = {}


def _load() -> None:
    for name in COLORS:
        try:
            filename = FILES.get(name, name)
            found = _PATH.findall((ICON_DIR / f"{filename}.svg").read_text())
        except OSError as exc:
            logger.warning("No brand mark for %s: %s", name, exc)
            continue
        if found:
            _paths[name] = " ".join(found)


_load()


def icon(platform: str, *, size: int = 14, title: str = "") -> Markup:
    """One brand mark as an inline ``<svg>``, or empty markup if we have none."""
    key = (platform or "").strip().lower()
    path = _paths.get(key)
    if not path:
        return Markup("")
    colour = COLORS.get(key) or "currentColor"
    label = title or ("X" if key == "twitter" else key)
    return Markup(
        f'<svg class="platform-icon" viewBox="0 0 24 24" width="{size}" height="{size}" '
        f'fill="{colour}" role="img" aria-label="{label}" focusable="false">'
        f'<title>{label}</title><path d="{path}"/></svg>'
    )


def known() -> list[str]:
    return sorted(_paths)
