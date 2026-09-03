"""``describe_image`` — letting the agent actually see a picture.

browse_page returns a URL, alt text and the surrounding caption; the image
itself never reaches the model. That is enough to pick a panel out of a numbered
list and useless for "which frame shows the letter?" — so the agent guessed from
filenames or gave up.

The tool does the deterministic half (resolve, fetch, validate, shrink) and
delegates only the looking, which is why almost all of this is testable without
a model.
"""
import asyncio

import pytest

from aismm.tools import vision_tool

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
JPEG = b"\xff\xd8\xff\xe0" + b"photo"


@pytest.fixture()
def described(monkeypatch):
    """Stub the vision agent; record what it was asked to look at."""
    seen = {}

    async def fake_describe(data, *, mime="image/jpeg", question="", source="", model=None):
        seen.update(bytes_len=len(data), mime=mime, question=question, source=source)
        return "A comic panel: a woman holds an unopened letter."

    monkeypatch.setattr("aismm.agent.vision.describe_image", fake_describe)
    return seen


def _run(target, question=""):
    return asyncio.run(vision_tool.perform_describe_image(target, question))


# --- looking at a saved asset --------------------------------------------------------- #

def test_a_saved_asset_is_described(described, monkeypatch):
    monkeypatch.setattr(vision_tool, "asset_exists", lambda p: True)
    monkeypatch.setattr(vision_tool, "read_bytes", lambda p: PNG)
    result = _run("/assets/panel.png", "what does the sign say?")
    assert "unopened letter" in result["description"]
    assert result["source"] == "/assets/panel.png"
    assert described["question"] == "what does the sign say?"


def test_the_media_type_is_sniffed_not_assumed(described, monkeypatch):
    """A PNG sent as image/jpeg is a rejected request, not a description."""
    monkeypatch.setattr(vision_tool, "asset_exists", lambda p: True)
    monkeypatch.setattr(vision_tool, "read_bytes", lambda p: PNG)
    _run("/assets/panel.jpg")            # the extension lies; the bytes win
    assert described["mime"] == "image/png"


def test_a_missing_asset_says_where_paths_come_from(described, monkeypatch):
    monkeypatch.setattr(vision_tool, "asset_exists", lambda p: False)
    result = _run("/assets/gone.png")
    assert result["error"] == "cannot_read"
    assert "save_media" in result["message"]


def test_an_empty_target_is_refused(described):
    assert _run("  ")["error"] == "no_target"


# --- looking at a URL ------------------------------------------------------------------ #

@pytest.fixture()
def http(monkeypatch):
    """Serve fixed bytes for any URL fetch."""
    state = {"data": PNG, "content_type": "image/png", "url": ""}

    class _Resp:
        status_code = 200

        def __init__(self):
            self.content = state["data"]
            self.headers = {"content-type": state["content_type"]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            state["url"] = url
            return _Resp()

    monkeypatch.setattr(vision_tool.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(vision_tool, "is_public_url", lambda u: (True, ""))
    return state


def test_a_public_image_url_is_fetched_and_described(described, http):
    result = _run("https://example.com/panel.png")
    assert "unopened letter" in result["description"]
    assert http["url"] == "https://example.com/panel.png"


def test_a_private_address_is_refused(described, monkeypatch):
    """Same SSRF guard as browsing — the agent picks the URL."""
    monkeypatch.setattr(vision_tool, "is_public_url",
                        lambda u: (False, "Refusing to browse a non-public address (127.0.0.1)."))
    result = _run("http://127.0.0.1/secret.png")
    assert result["error"] == "cannot_read"
    assert "non-public" in result["message"]
    assert described == {}                 # nothing was sent to the model


def test_an_html_page_is_not_described_as_an_image(described, http):
    """A 404 page served at a .png URL must not become a hallucinated description."""
    http["data"] = b"<!DOCTYPE html><html>not found</html>"
    http["content_type"] = "text/html"
    result = _run("https://example.com/missing.png")
    assert result["error"] == "cannot_read"
    assert described == {}


def test_a_video_is_refused_with_a_reason(described, http):
    http["data"] = b"\x00\x00\x00\x18ftypmp42" + b"0" * 32
    http["content_type"] = "video/mp4"
    result = _run("https://example.com/clip.mp4")
    assert result["error"] == "cannot_read"
    assert "only looks at images" in result["message"]


def test_a_download_failure_is_reported_not_raised(described, monkeypatch):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(vision_tool.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(vision_tool, "is_public_url", lambda u: (True, ""))
    result = _run("https://example.com/panel.png")
    assert result["error"] == "cannot_read"
    assert "connection reset" in result["message"]


# --- size ------------------------------------------------------------------------------ #

def test_an_enormous_image_is_shrunk_before_it_is_sent(described, monkeypatch):
    """The model does not see the extra pixels; the upload time is real."""
    big = JPEG + b"0" * (vision_tool._MAX_SEND_BYTES + 1)
    monkeypatch.setattr(vision_tool, "asset_exists", lambda p: True)
    monkeypatch.setattr(vision_tool, "read_bytes", lambda p: big)
    monkeypatch.setattr(vision_tool.media, "normalize_image",
                        lambda data, **kw: b"\xff\xd8\xffsmall")
    result = _run("/assets/huge.jpg")
    assert described["bytes_len"] == len(b"\xff\xd8\xffsmall")
    assert result["bytes"] == len(b"\xff\xd8\xffsmall")


def test_a_failed_shrink_still_sends_the_original(described, monkeypatch):
    big = JPEG + b"0" * (vision_tool._MAX_SEND_BYTES + 1)
    monkeypatch.setattr(vision_tool, "asset_exists", lambda p: True)
    monkeypatch.setattr(vision_tool, "read_bytes", lambda p: big)

    def _boom(data, **kw):
        raise RuntimeError("pillow said no")

    monkeypatch.setattr(vision_tool.media, "normalize_image", _boom)
    assert "description" in _run("/assets/huge.jpg")


def test_something_far_too_large_is_not_downloaded_into_the_model(described, http):
    http["data"] = PNG + b"0" * vision_tool._MAX_DOWNLOAD_BYTES
    result = _run("https://example.com/huge.png")
    assert result["error"] == "cannot_read"
    assert "too large" in result["message"]


# --- failure must not kill the run ------------------------------------------------------ #

def test_a_model_failure_tells_the_agent_it_can_carry_on(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("deployment has no vision support")

    monkeypatch.setattr("aismm.agent.vision.describe_image", boom)
    monkeypatch.setattr(vision_tool, "asset_exists", lambda p: True)
    monkeypatch.setattr(vision_tool, "read_bytes", lambda p: PNG)
    result = _run("/assets/panel.png")
    assert result["error"] == "vision_failed"
    assert "Carry on without it" in result["message"]


def test_an_empty_description_is_an_error_not_a_result(monkeypatch):
    async def nothing(*a, **kw):
        return ""

    monkeypatch.setattr("aismm.agent.vision.describe_image", nothing)
    monkeypatch.setattr(vision_tool, "asset_exists", lambda p: True)
    monkeypatch.setattr(vision_tool, "read_bytes", lambda p: PNG)
    assert _run("/assets/panel.png")["error"] == "vision_failed"


# --- registration ----------------------------------------------------------------------- #

def test_the_tool_is_registered_and_offered():
    from aismm.tools.registry import registered_tool_names

    assert "describe_image" in registered_tool_names()


def test_the_tool_is_hidden_when_no_llm_is_configured(monkeypatch):
    """A tool that could only ever fail should not be offered."""
    class _LLM:
        azure_api_key = ""
        apim_subscription_key = ""

    class _Settings:                      # Settings is frozen; swap the whole thing
        llm = _LLM()

    monkeypatch.setattr(vision_tool, "settings", _Settings())
    assert vision_tool._make_describe_image({}) is None


def test_the_tool_appears_in_the_dashboard_picker():
    from aismm.dashboard.app import TOOL_GROUPS

    assert any("describe_image" in names for _t, _b, _p, names in TOOL_GROUPS)


def test_the_agent_is_told_it_can_look_at_images():
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS

    assert "describe_image" in MANAGER_INSTRUCTIONS


# --- it must not be used to proof-read our own output --------------------------------- #
# Reported: an image generation run FAILED because the agent checked its own
# output with describe_image, which read a correct Persian footer as garbled and
# a correct phone number as malformed. Both images were fine. A verifier less
# reliable than the thing it verifies will veto good work.

def test_the_tool_warns_against_proof_reading_generated_images():
    import inspect

    source = inspect.getsource(vision_tool)
    assert "Do not use this to proof-read an image you generated" in source
    # ...and says WHY, so the caution survives an edit that shortens it.
    for reason in ("phone numbers", "non-Latin scripts", "right-to-left"):
        assert reason in source


def test_the_tool_no_longer_advertises_checking_your_own_image():
    """This line is what invited the behaviour in the first place."""
    import inspect

    source = inspect.getsource(vision_tool)
    assert "came out right" not in source


def test_the_prompt_says_the_same():
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS as p

    assert "NEVER use it to proof-read an image YOU generated" in p
    assert "came out as asked" not in p


def test_the_prompt_forbids_inventing_acceptance_tests():
    """The failures listed are failures of INPUT. Producing an image you asked
    for correctly is finished work."""
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS as p

    assert "Do not\ninvent extra acceptance tests of your own OUTPUT" in p
    assert "let\nthe human looking at the approval queue decide" in p
