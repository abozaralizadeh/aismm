"""Guards for the mobile layout.

Measured at a 375px viewport before this was fixed: the page scrolled sideways by
468px, the nav was clipped off the right edge, a six-column table was 842px wide
with no scroll container, and form controls rendered at 13–14px — which makes iOS
Safari zoom in on focus and never zoom back.

These are static checks on the templates and stylesheet, not a browser run: cheap
enough for every commit, and they catch the specific regressions (an unwrapped
table, a missing viewport tag, a sub-16px control) that make the dashboard
unusable on a phone.
"""
import re
from pathlib import Path

import pytest

TEMPLATES = Path("aismm/dashboard/templates")
CSS = Path("aismm/dashboard/static/style.css").read_text()
TEMPLATE_FILES = sorted(TEMPLATES.glob("*.html"))
WITH_TABLES = [p for p in TEMPLATE_FILES if "<table" in p.read_text()]


# --- the viewport tag: without it a phone renders at 980px and scales down ---------- #

@pytest.mark.parametrize("template", ["base.html", "login.html"])
def test_pages_declare_a_mobile_viewport(template):
    html = (TEMPLATES / template).read_text()
    assert 'name="viewport"' in html
    assert "width=device-width" in html


# --- tables must scroll inside their own box, never widen the page ------------------ #

def test_there_are_tables_to_guard():
    """Sanity: if this list ever empties, the checks below are vacuous."""
    assert WITH_TABLES


@pytest.mark.parametrize("template", WITH_TABLES, ids=lambda p: p.name)
def test_every_table_is_wrapped_in_a_scroll_container(template):
    html = template.read_text()
    opens = len(re.findall(r"<table\b", html))
    wrappers = len(re.findall(r'<div class="table-scroll">', html))
    assert wrappers >= opens, (
        f"{template.name} has {opens} table(s) but {wrappers} .table-scroll wrapper(s). "
        f"An unwrapped table makes the whole page scroll sideways on a phone."
    )


def test_the_scroll_container_actually_scrolls():
    block = _rule(".table-scroll")
    assert "overflow-x: auto" in block


# --- form controls at 16px on touch, or iOS zooms the page ------------------------- #

def test_touch_devices_get_16px_controls():
    """The zoom-on-focus trap. Keyed on pointer:coarse so iPad portrait is covered."""
    assert "@media (pointer: coarse)" in CSS
    touch_block = CSS.split("@media (pointer: coarse)", 1)[1]
    assert "font-size: 16px" in touch_block


def test_narrow_screens_also_get_16px_controls():
    narrow = _media_block("max-width: 720px")
    assert "font-size: 16px" in narrow


# Types that put a keyboard on screen — these are the ones iOS zooms into.
# checkbox/radio/file/submit don't focus a text field, so they're exempt.
ZOOMABLE_INPUT_TYPES = {"text", "password", "email", "number", "search", "tel",
                        "url", "date", "datetime-local", "time", "month", "week"}


def _form_input_types() -> set[str]:
    """Every zoomable input type the templates actually use."""
    found = set()
    for path in TEMPLATE_FILES:
        for match in re.finditer(r'<input[^>]*type="([^"]+)"', path.read_text()):
            if match.group(1) in ZOOMABLE_INPUT_TYPES:
                found.add(match.group(1))
    return found


def test_the_templates_use_input_types_worth_checking():
    """Guards the guard: if this is empty the two tests below prove nothing."""
    assert _form_input_types()


@pytest.mark.parametrize("block", ["@media (pointer: coarse)", "max-width: 720px"])
def test_every_form_input_type_is_named_in_the_16px_rules(block):
    """A bare `input` selector is NOT enough here.

    ``.form input[type=text]`` in the base rule sets ``font: inherit`` (15px) and
    out-specifies a bare ``input { font-size: 16px }`` inside a media query — so
    each type has to be listed explicitly or it silently keeps the zooming size.
    A datetime-local field shipped at 15px exactly this way.
    """
    css = (CSS.split("@media (pointer: coarse)", 1)[1] if block.startswith("@media")
           else _media_block(block))
    for input_type in _form_input_types():
        # Scoped either way (.form … or .filters …) — what matters is that the
        # type is named, so it beats the base rule's `font: inherit`.
        assert f"input[type={input_type}]" in css, (
            f"input[type={input_type}] is used in a template but is not listed in the "
            f"16px rules for {block} — it will render below 16px and zoom on iOS")


# --- the topbar nav must never be clipped ------------------------------------------ #

def test_the_nav_scrolls_rather_than_clipping():
    nav = _rule(".topbar nav")
    assert "overflow-x: auto" in nav
    assert "flex-wrap: wrap" in _rule(".topbar")


def test_nav_links_do_not_wrap_mid_word():
    assert "white-space: nowrap" in _rule(".topbar nav a")


# --- stacking and tap targets ------------------------------------------------------- #

def test_filters_stack_on_narrow_screens():
    narrow = _media_block("max-width: 720px")
    assert ".filters" in narrow and "flex-direction: column" in narrow


def test_tap_targets_reach_44px():
    narrow = _media_block("max-width: 720px")
    assert "min-height: 44px" in narrow


def test_nothing_is_sticky_on_a_short_screen():
    """A sticky sidebar eats most of a phone viewport."""
    narrow = _media_block("max-width: 720px")
    assert ".split-aside" in narrow and "position: static" in narrow


def test_long_ids_and_paths_wrap():
    """Run ids, asset paths and blob URLs would otherwise widen the page."""
    narrow = _media_block("max-width: 720px")
    assert "overflow-wrap: anywhere" in narrow


def test_ios_text_inflation_is_disabled():
    assert "-webkit-text-size-adjust: 100%" in CSS


def test_multi_column_layouts_collapse():
    assert "@media (max-width: 640px)" in CSS      # .grid-2
    assert "@media (max-width: 860px)" in CSS      # .split
    assert "grid-template-columns: 1fr" in _media_block("max-width: 860px")


# --- helpers ----------------------------------------------------------------------- #

def _rule(selector: str) -> str:
    """The declaration block for the first top-level rule matching ``selector``."""
    pattern = re.compile(re.escape(selector) + r"\s*\{([^}]*)\}")
    match = pattern.search(CSS)
    assert match, f"no CSS rule found for {selector!r}"
    return match.group(1)


def _media_block(query: str) -> str:
    """Everything inside the first ``@media`` block containing ``query``."""
    start = CSS.find(f"@media ({query})")
    assert start != -1, f"no @media block for {query!r}"
    depth, index = 0, CSS.index("{", start)
    for position in range(index, len(CSS)):
        if CSS[position] == "{":
            depth += 1
        elif CSS[position] == "}":
            depth -= 1
            if depth == 0:
                return CSS[index:position]
    raise AssertionError(f"unbalanced braces after {query!r}")
