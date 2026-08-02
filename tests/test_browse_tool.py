"""Browsing tools: URL safety, media download, graceful absence of Playwright.

No browser is launched here — the Playwright parts are exercised through their
guards and the download path, which is plain httpx.
"""
import asyncio

import httpx
import pytest

from aismm.tools import browse_tool


# --- SSRF guard ----------------------------------------------------------------- #
# The agent picks the URL, so an unguarded fetcher on a cloud VM would happily
# read the instance metadata endpoint and hand its credentials to the model.

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/metadata/instance",   # Azure/AWS instance metadata
    "http://127.0.0.1:8787/settings",             # the dashboard itself
    "http://localhost/admin",
    "http://10.0.0.5/internal",
    "http://192.168.1.1/",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "not-a-url",
])
def test_non_public_urls_are_refused(url):
    ok, why = browse_tool.is_public_url(url)
    assert ok is False and why


def test_public_https_url_is_allowed():
    ok, why = browse_tool.is_public_url("https://example.com/news")
    assert ok is True and why == ""


def test_unresolvable_host_is_refused():
    ok, why = browse_tool.is_public_url("https://nx-does-not-exist.invalid/")
    assert ok is False
    assert "resolve" in why.lower()


# --- save_media ------------------------------------------------------------------ #

def _save_media(monkeypatch, tmp_path, *, status=200, content_type="image/jpeg",
                body=b"\xff\xd8binary", url="https://cdn.example.com/a.jpg",
                real_guard=False):
    """Run perform_save_media against a mocked HTTP response.

    The SSRF guard does a real DNS lookup, which a made-up test host would fail;
    it has its own tests above, so it is stubbed out unless a test wants it.
    """
    state = {"assets": []}
    if not real_guard:
        monkeypatch.setattr(browse_tool, "is_public_url", lambda u: (True, ""))

    def handler(request):
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(browse_tool.httpx, "AsyncClient",
                        lambda *a, **kw: real_client(*a, **{**kw, "transport": transport}))
    monkeypatch.setattr(browse_tool, "save_bytes",
                        lambda data, ext: str(tmp_path / f"asset.{ext}"))
    monkeypatch.setattr(browse_tool, "public_url", lambda p: f"https://host/assets/{p}")

    result = asyncio.run(browse_tool.perform_save_media(state, url))
    return result, state


def test_image_is_downloaded_and_recorded_as_an_asset(monkeypatch, tmp_path):
    result, state = _save_media(monkeypatch, tmp_path)
    assert result["kind"] == "image"
    assert result["asset_path"].endswith(".jpg")
    assert state["assets"][0]["source_url"] == "https://cdn.example.com/a.jpg"


def test_video_content_type_is_recognised(monkeypatch, tmp_path):
    result, _ = _save_media(monkeypatch, tmp_path, content_type="video/mp4",
                            url="https://cdn.example.com/a.mp4")
    assert result["kind"] == "video"
    assert result["asset_path"].endswith(".mp4")


def test_html_is_not_saved_as_media(monkeypatch, tmp_path):
    result, state = _save_media(monkeypatch, tmp_path, content_type="text/html",
                                body=b"<html></html>")
    assert result["error"] == "unsupported_media"
    assert state["assets"] == []


def test_download_error_is_reported_not_raised(monkeypatch, tmp_path):
    result, _ = _save_media(monkeypatch, tmp_path, status=404)
    assert result["error"] == "download_failed"


def test_save_media_refuses_a_private_url(monkeypatch, tmp_path):
    result, state = _save_media(monkeypatch, tmp_path, url="http://169.254.169.254/latest",
                                real_guard=True)
    assert result["error"] == "url_not_allowed"
    assert state["assets"] == []


# --- availability ----------------------------------------------------------------- #

def test_tools_disable_themselves_without_playwright(monkeypatch):
    """Same convention as Sora: an unconfigured capability yields no tool."""
    monkeypatch.setattr(browse_tool, "playwright_available", lambda: False)
    assert browse_tool._make_browse_page({}) is None
    assert browse_tool._make_save_media({}) is None


def test_close_browser_is_safe_when_nothing_was_started():
    asyncio.run(browse_tool.close_browser({}))   # must not raise


def test_close_browser_swallows_teardown_errors():
    class Boom:
        async def close(self):
            raise RuntimeError("already gone")

    class Stopper:
        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

    stopper = Stopper()
    state = {"_browser": Boom(), "_playwright": stopper}
    asyncio.run(browse_tool.close_browser(state))    # error must not propagate
    assert stopper.stopped is True                   # ...and stop() still ran


# --- image extraction shape ---------------------------------------------------------- #

@pytest.mark.parametrize("image,expected", [
    ({"src": "https://x/favicon.ico", "width": 512, "height": 512}, True),
    ({"src": "https://x/static/icons/a.png", "width": 512, "height": 512}, True),
    ({"src": "https://x/pixel.gif", "width": 1, "height": 1}, True),      # tracking pixel
    ({"src": "", "width": 900, "height": 900}, True),                     # never loaded
    ({"src": "https://x/panel1.jpg", "width": 1536, "height": 1024}, False),
    ({"src": "https://x/panel2.jpg", "width": 0, "height": 0}, False),    # unknown size: keep
])
def test_decorative_images_are_filtered(image, expected):
    assert browse_tool._is_decorative(image) is expected


def test_extraction_js_prefers_the_full_resolution_source():
    """A thumbnail in src often has the real asset in data-full/srcset."""
    js = browse_tool._EXTRACT_IMAGES_JS
    for attr in ("data-full", "data-src", "data-original", "data-lazy-src"):
        assert attr in js
    assert "srcset" in js
    assert "alt" in js and "caption" in js


def test_lazy_images_are_forced_to_load():
    """loading=lazy images never populate unless eagerly loaded or scrolled to."""
    js = browse_tool._LOAD_IMAGES_JS
    assert 'loading="lazy"' in js and "eager" in js
    assert "scrollTo" in js


# --- media sniffing ------------------------------------------------------------------ #
# Storage written without a content type serves application/octet-stream. The
# project's own comic-panel blobs do exactly that, and trusting the header meant
# refusing real PNGs.

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 20
MP4 = b"\x00\x00\x00\x20" + b"ftyp" + b"isom" + b"\x00" * 20
MOV = b"\x00\x00\x00\x20" + b"ftyp" + b"qt  " + b"\x00" * 20
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 32


@pytest.mark.parametrize("data,kind,ext", [
    (PNG, "image", "png"), (JPEG, "image", "jpg"), (GIF, "image", "gif"),
    (WEBP, "image", "webp"), (MP4, "video", "mp4"), (MOV, "video", "mov"),
    (WEBM, "video", "webm"),
])
def test_magic_bytes_identify_media_regardless_of_content_type(data, kind, ext):
    got_kind, got_ext, how = browse_tool.sniff_media(data, "application/octet-stream", "")
    assert (got_kind, got_ext) == (kind, ext)
    assert how.startswith("magic:")


def test_the_real_failure_case_a_png_served_as_octet_stream():
    """Exactly what the comic-panel blob returns."""
    kind, ext, _ = browse_tool.sniff_media(
        PNG, "application/octet-stream",
        "https://pkrstr.blob.core.windows.net/comicbook-html/20260513_0335_da11.png")
    assert (kind, ext) == ("image", "png")


def test_bytes_win_over_a_wrong_content_type():
    kind, ext, how = browse_tool.sniff_media(PNG, "image/jpeg", "x.jpg")
    assert (kind, ext) == ("image", "png")
    assert how == "magic:png"


def test_content_type_is_used_when_bytes_are_inconclusive():
    kind, ext, how = browse_tool.sniff_media(b"\x00" * 64, "image/jpeg", "")
    assert (kind, ext) == ("image", "jpg")
    assert how.startswith("content-type:")


def test_url_extension_is_the_last_resort():
    kind, ext, how = browse_tool.sniff_media(b"\x00" * 64, "application/octet-stream",
                                             "https://x/y/panel.mp4?sig=abc")
    assert (kind, ext) == ("video", "mp4")
    assert how.startswith("url-extension:")


@pytest.mark.parametrize("data,content_type,url", [
    (b"<html><body>nope</body></html>", "text/html", "https://x/page"),
    (b"%PDF-1.7 something", "application/pdf", "https://x/doc.pdf"),
    (b"\x00" * 64, "application/octet-stream", "https://x/mystery"),
])
def test_non_media_is_still_refused(data, content_type, url):
    kind, ext, why = browse_tool.sniff_media(data, content_type, url)
    assert (kind, ext) == ("", "")
    assert why


def test_save_media_accepts_an_octet_stream_png(monkeypatch, tmp_path):
    """The end-to-end regression: this used to fail the whole run."""
    result, state = _save_media(monkeypatch, tmp_path, content_type="application/octet-stream",
                                body=PNG, url="https://cdn.example.com/panel.png")
    assert result["kind"] == "image"
    assert result["asset_path"].endswith(".png")
    assert state["assets"][0]["kind"] == "image"


# --- content that does not exist until you click it ---------------------------------- #
# The live failure: a comic page kept its character reference sheet in a modal
# whose <img> had NO src attribute at all until a button set it. No amount of
# waiting could reveal it, and the button was a <button>, so it never appeared
# under `links` either — the agent could neither see the image nor discover the
# control, and correctly reported it could not complete the post.

class _FakePage:
    """Playwright page stand-in whose DOM changes only when clicked."""

    def __init__(self, *, click_selector="#charSheetLink", click_raises=False):
        self._click_selector = click_selector
        self._click_raises = click_raises
        self.clicked = []
        self.revealed = False

    async def goto(self, *a, **kw):
        return None

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def wait_for_selector(self, *a, **kw):
        return None

    async def wait_for_timeout(self, *a, **kw):
        return None

    async def click(self, selector, **kw):
        if self._click_raises or selector != self._click_selector:
            raise RuntimeError(f"no element matches {selector}")
        self.clicked.append(selector)
        self.revealed = True

    async def title(self):
        return "ComicBook"

    async def evaluate(self, script):
        # The image-extraction script also mentions innerText (it reads captions),
        # so key on the text script's own selector instead.
        if "article, main" in script:
            return "Episode text"
        if "loading" in script or "scrollTo" in script:      # force-lazy-images
            return None
        images = [{"src": f"https://cdn/panel{n}.png", "alt": f"Panel {n}",
                   "width": 1024, "height": 1024} for n in (1, 2)]
        if self.revealed:
            images.append({"src": "https://cdn/charsheet.png", "alt": "",
                           "width": 1536, "height": 1024})
        return images

    async def eval_on_selector_all(self, selector, script):
        if selector.startswith("a[href]"):
            return [{"text": "Home", "href": "https://genbox/"}]
        if "button" in selector:
            return [{"label": "Characters", "selector": "#charSheetLink"},
                    {"label": "Next", "selector": "#btnNext"}]
        return []

    async def close(self):
        return None


def _browser_with(page, monkeypatch):
    from aismm.tools import browse_tool

    class _Browser:
        async def new_page(self):
            return page

    async def fake_get_browser(_state):
        return _Browser()

    monkeypatch.setattr(browse_tool, "get_browser", fake_get_browser)
    # These tests are about the DOM, not the SSRF guard, which would refuse the
    # made-up hostnames below before the page was ever opened.
    monkeypatch.setattr(browse_tool, "is_public_url", lambda url: (True, ""))


def test_a_modal_image_is_invisible_without_a_click(monkeypatch):
    """Reproduces exactly what the agent hit."""
    from aismm.tools.browse_tool import perform_browse_page

    page = _FakePage()
    _browser_with(page, monkeypatch)
    result = asyncio.run(perform_browse_page({}, "https://genbox/comicbook?date=2026-05-25"))

    assert [i["alt"] for i in result["images"]] == ["Panel 1", "Panel 2"]
    assert page.clicked == []


def test_clicking_reveals_it(monkeypatch):
    from aismm.tools.browse_tool import perform_browse_page

    page = _FakePage()
    _browser_with(page, monkeypatch)
    result = asyncio.run(perform_browse_page(
        {}, "https://genbox/comicbook?date=2026-05-25", click="#charSheetLink"))

    assert page.clicked == ["#charSheetLink"]
    assert result["clicked"] == "#charSheetLink"
    assert any("charsheet" in i["url"] for i in result["images"])


def test_buttons_are_reported_so_the_control_is_discoverable(monkeypatch):
    """Without this the agent cannot even know a Characters button exists —
    buttons are not links, so they never show up under `links`."""
    from aismm.tools.browse_tool import perform_browse_page

    _browser_with(_FakePage(), monkeypatch)
    result = asyncio.run(perform_browse_page({}, "https://genbox/comicbook"))

    selectors = [b["selector"] for b in result["buttons"]]
    assert "#charSheetLink" in selectors
    assert all(b["href"] != "#charSheetLink" for b in result["links"])


def test_a_click_that_matches_nothing_is_reported_not_fatal(monkeypatch):
    """The page is still read; the agent is told to check `buttons`."""
    from aismm.tools.browse_tool import perform_browse_page

    page = _FakePage(click_raises=True)
    _browser_with(page, monkeypatch)
    result = asyncio.run(perform_browse_page({}, "https://genbox/x", click="#nope"))

    assert result["clicked"] == ""
    assert "buttons" in result["click_failed"]
    assert result["images"], "the page should still have been read"


def test_no_click_key_when_none_was_asked_for(monkeypatch):
    from aismm.tools.browse_tool import perform_browse_page

    _browser_with(_FakePage(), monkeypatch)
    result = asyncio.run(perform_browse_page({}, "https://genbox/x"))
    assert "clicked" not in result and "click_failed" not in result


def test_the_tool_docstring_points_at_buttons_when_something_is_missing():
    """That is the discovery path; it has to be in what the model reads."""
    from aismm.tools import browse_tool

    state = {}
    tool = browse_tool._make_browse_page(state)
    if tool is None:
        pytest.skip("playwright not installed")
    doc = tool.description if hasattr(tool, "description") else tool.__doc__
    assert "buttons" in doc
    assert "click" in doc
