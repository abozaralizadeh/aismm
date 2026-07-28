"""``browse_page`` / ``save_media`` — read real web pages and keep their media.

Playwright drives a headless Chromium, so JavaScript-rendered pages work — the
same engine SandBox/AIBlog uses (``AIBlog/tools/browseweb.py``), minus the
LangChain toolkit, since AISMM's tools are Agents-SDK ``@function_tool``s. It is
free and self-hosted; no API key, no per-call cost.

Two lessons carried over from AIBlog:

* **Tear the browser down inside the same event loop.** A Chromium subprocess
  finalized by GC later raises "Event loop is closed". The browser is cached on
  the run ``state`` and closed by ``manager_agent`` in a ``finally``.
* **Chromium binaries are a separate install** (``playwright install chromium``).
  Without them the factory returns ``None`` and the agent simply works without
  the tool, as with Sora when unconfigured.

Fetched media is written into the assets dir like generated media, so a browsed
image or video can be passed straight to ``publish``.

SSRF guard: the agent chooses the URL, so private, loopback and link-local
addresses are refused — on a cloud VM ``169.254.169.254`` would otherwise hand
the instance metadata (and its credentials) to the model.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse

import httpx
from agents import function_tool

from ..assets import public_url, save_bytes
from .registry import register_tool

logger = logging.getLogger("aismm.tools.browse")

_NAV_TIMEOUT_MS = 45_000
_TEXT_LIMIT = 12_000          # chars of page text returned to the model
_LINK_LIMIT = 60
_MEDIA_LIMIT = 40
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
_VIDEO_TYPES = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


# --- URL safety --------------------------------------------------------------- #

def is_public_url(url: str) -> tuple[bool, str]:
    """Reject anything that isn't a public http(s) address (SSRF guard)."""
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return False, "Only http(s) URLs are allowed."
    host = parsed.hostname
    if not host:
        return False, "URL has no host."
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"Could not resolve host: {exc}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, f"Refusing to browse a non-public address ({ip})."
    return True, ""


# --- browser lifecycle -------------------------------------------------------- #

async def get_browser(state: dict):
    """Lazily launch one Chromium per run and cache it on ``state``."""
    if state.get("_browser") is None:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        state["_playwright"] = playwright
        state["_browser"] = browser
        logger.info("Playwright Chromium started")
    return state["_browser"]


async def close_browser(state: dict) -> None:
    """Close the run's browser. Call from a ``finally`` in the same event loop."""
    browser, playwright = state.pop("_browser", None), state.pop("_playwright", None)
    try:
        if browser is not None:
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - teardown must not mask a run error
        logger.warning("Browser close failed: %s", exc)
    finally:
        try:
            if playwright is not None:
                await playwright.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Playwright stop failed: %s", exc)


# --- tools --------------------------------------------------------------------- #

async def perform_browse_page(state: dict, url: str, scroll: bool = True) -> dict:
    """Render one page and extract text/links/media (extracted for testability)."""
    ok, why = is_public_url(url)
    if not ok:
        return {"error": "url_not_allowed", "message": why}
    try:
        browser = await get_browser(state)
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            if scroll:
                await page.evaluate(
                    "async () => { for (let y = 0; y < document.body.scrollHeight; y += 800)"
                    " { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); } }")
            title = await page.title()
            text = await page.evaluate(
                "() => (document.querySelector('article, main') || document.body).innerText")
            links = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => ({text: e.innerText.trim().slice(0, 120),"
                           " href: e.href})).filter(l => l.href.startsWith('http'))")
            images = await page.eval_on_selector_all(
                "img", "els => els.map(e => e.currentSrc || e.src).filter(Boolean)")
            videos = await page.eval_on_selector_all(
                "video, video source",
                "els => els.map(e => e.currentSrc || e.src).filter(Boolean)")
        finally:
            await page.close()
    except Exception as exc:  # noqa: BLE001 - report, don't kill the run
        logger.warning("browse_page failed for %s: %s", url, exc)
        return {"error": "browse_failed", "message": f"{type(exc).__name__}: {exc}"}

    text = (text or "").strip()
    truncated = len(text) > _TEXT_LIMIT
    # De-duplicate while preserving order, resolving relative URLs.
    seen: set[str] = set()

    def _uniq(items, limit):
        out = []
        for item in items:
            absolute = urljoin(url, item)
            if absolute not in seen:
                seen.add(absolute)
                out.append(absolute)
            if len(out) >= limit:
                break
        return out

    return {
        "url": url,
        "title": title,
        "text": text[:_TEXT_LIMIT],
        "text_truncated": truncated,
        "links": [{"text": l["text"], "href": l["href"]} for l in (links or [])[:_LINK_LIMIT]],
        "images": _uniq(images or [], _MEDIA_LIMIT),
        "videos": _uniq(videos or [], _MEDIA_LIMIT),
    }


def _make_browse_page(state: dict):
    if not playwright_available():
        return None

    @function_tool
    async def browse_page(url: str, scroll: bool = True) -> dict:
        """Open a web page in a real browser and read it.

        Runs JavaScript, so it works on pages that a plain HTTP fetch returns
        empty. Returns the page title, its visible text, the links it contains,
        and the image/video URLs found on it. Pass an image or video URL to
        ``save_media`` to download it for posting.

        Args:
            url: Absolute http(s) URL to open.
            scroll: Scroll to the bottom first so lazy-loaded images and
                infinite-scroll items are present. Leave true for feeds and
                article lists.
        """
        return await perform_browse_page(state, url, scroll)

    return browse_page


async def perform_save_media(state: dict, url: str) -> dict:
    """Download one image/video into the assets dir (extracted for testability)."""
    ok, why = is_public_url(url)
    if not ok:
        return {"error": "url_not_allowed", "message": why}
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (AISMM)"})
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            data = resp.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("save_media failed for %s: %s", url, exc)
        return {"error": "download_failed", "message": f"{type(exc).__name__}: {exc}"}

    ext = _IMAGE_TYPES.get(content_type) or _VIDEO_TYPES.get(content_type)
    if not ext:
        return {"error": "unsupported_media",
                "message": f"{url} is {content_type or 'an unknown type'}, not an image or video."}
    if len(data) > _MAX_DOWNLOAD_BYTES:
        return {"error": "too_large",
                "message": f"{len(data)} bytes exceeds the {_MAX_DOWNLOAD_BYTES} byte limit."}

    kind = "image" if content_type in _IMAGE_TYPES else "video"
    path = save_bytes(data, ext)
    asset = {"path": path, "kind": kind, "public_url": public_url(path),
             "source_url": url, "bytes": len(data)}
    state.setdefault("assets", []).append(asset)
    logger.info("Saved %s from %s (%d bytes)", kind, url, len(data))
    return {"asset_path": path, "kind": kind, "public_url": asset["public_url"],
            "bytes": len(data), "source_url": url}


def _make_save_media(state: dict):
    if not playwright_available():
        return None

    @function_tool
    async def save_media(url: str) -> dict:
        """Download an image or video found on a page so it can be posted.

        Use with an image/video URL returned by ``browse_page``. The file is
        stored alongside generated media; pass the returned ``asset_path`` to
        ``publish``.

        Only real image/video responses are kept — an HTML page or a PDF is
        rejected rather than saved as a broken asset.
        """
        return await perform_save_media(state, url)

    return save_media


register_tool("browse_page", _make_browse_page)
register_tool("save_media", _make_save_media)
