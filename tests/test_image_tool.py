"""Image generation options and the gpt-image-1 / gpt-image-2 differences.

No API calls: the client is stubbed, so these pin how our parameters are built —
which is where the model-specific traps live (gpt-image-2 rejects
``input_fidelity`` and transparency; gpt-image-1 only accepts three sizes).
"""
import asyncio
import base64
import dataclasses

import pytest

from aismm import config as config_module
from aismm.config import ImageSettings
from aismm.tools import image_tool


@pytest.fixture()
def captured(monkeypatch, tmp_path):
    """Capture the kwargs sent to the API; return them plus the tool result."""
    calls = {}

    class FakeImages:
        async def generate(self, **kwargs):
            calls["endpoint"] = "generate"
            calls["kwargs"] = kwargs
            return _fake_response()

        async def edit(self, **kwargs):
            calls["endpoint"] = "edit"
            calls["kwargs"] = kwargs
            return _fake_response()

    class FakeClient:
        images = FakeImages()

    monkeypatch.setattr(image_tool, "_client", lambda: FakeClient())
    monkeypatch.setattr(image_tool, "save_bytes",
                        lambda data, ext: str(tmp_path / f"out.{ext}"))
    monkeypatch.setattr(image_tool, "public_url", lambda p: f"https://host/{p}")
    monkeypatch.setattr(image_tool, "read_bytes", lambda p: b"\x89PNG\r\n\x1a\nfake")
    return calls


def _fake_response():
    class Datum:
        b64_json = base64.b64encode(b"image-bytes").decode()

    class Response:
        data = [Datum()]

    return Response()


def _with_model(monkeypatch, model):
    monkeypatch.setattr(image_tool, "settings", dataclasses.replace(
        config_module.settings,
        image=ImageSettings(api_key="k", endpoint="https://e", model=model)))


def _run(state=None, **kwargs):
    # `state or {}` would replace a caller's EMPTY dict, losing the mutations
    # the tool records on it.
    return asyncio.run(image_tool.perform_generate_image(
        state if state is not None else {}, "a cat", **kwargs))


# --- size resolution ---------------------------------------------------------------- #

@pytest.mark.parametrize("orientation,expected", [
    ("portrait", "1024x1536"), ("landscape", "1536x1024"), ("square", "1024x1024"),
])
def test_orientation_presets(orientation, expected):
    assert image_tool.resolve_size("", orientation, "gpt-image-2")[0] == expected


@pytest.mark.parametrize("preset,expected", [
    ("1k", "1024x1024"), ("2k", "2048x2048"), ("4k", "3840x2160"), ("auto", "auto"),
])
def test_named_presets(preset, expected):
    assert image_tool.resolve_size(preset, "square", "gpt-image-2")[0] == expected


def test_custom_size_is_accepted_on_gpt_image_2():
    """1440x1792 — both edges already multiples of 16, so nothing is adjusted."""
    size, note = image_tool.resolve_size("1440x1792", "square", "gpt-image-2")
    assert size == "1440x1792" and note == ""


def test_a_nearly_valid_size_is_rounded_not_rejected():
    """1800 is not a multiple of 16, so it is nudged to the nearest one."""
    size, note = image_tool.resolve_size("1440x1800", "square", "gpt-image-2")
    width, height = (int(v) for v in size.split("x"))
    assert width == 1440 and height % 16 == 0 and abs(height - 1800) <= 16
    assert "multiple of 16" in note


def test_edges_are_rounded_to_a_multiple_of_16():
    """gpt-image-2 requires it; a raw 1441 would just fail."""
    size, note = image_tool.resolve_size("1441x1801", "square", "gpt-image-2")
    width, height = (int(v) for v in size.split("x"))
    assert width % 16 == 0 and height % 16 == 0
    assert "multiple of 16" in note


def test_extreme_aspect_ratio_is_clamped():
    size, note = image_tool.resolve_size("4096x256", "square", "gpt-image-2")
    width, height = (int(v) for v in size.split("x"))
    assert max(width, height) / min(width, height) <= 3.0
    assert "aspect ratio" in note


def test_gpt_image_1_rejects_custom_sizes_with_an_explanation():
    size, note = image_tool.resolve_size("1440x1792", "square", "gpt-image-1")
    assert size == "1024x1024"
    assert "only accepts" in note


def test_gpt_image_1_accepts_its_own_sizes():
    assert image_tool.resolve_size("1024x1536", "square", "gpt-image-1") == ("1024x1536", "")


@pytest.mark.parametrize("bad", ["huge", "1024", "axb"])
def test_nonsense_size_falls_back_rather_than_failing(bad):
    size, note = image_tool.resolve_size(bad, "square", "gpt-image-2")
    assert size == "1024x1024" and note


# --- request construction ------------------------------------------------------------ #

def test_quality_and_format_are_passed(monkeypatch, captured):
    _with_model(monkeypatch, "gpt-image-2")
    _run(quality="high", output_format="jpeg")
    assert captured["kwargs"]["quality"] == "high"
    assert captured["kwargs"]["output_format"] == "jpeg"


def test_auto_quality_is_omitted(monkeypatch, captured):
    """"auto" is the API default; sending it adds nothing."""
    _with_model(monkeypatch, "gpt-image-2")
    _run(quality="auto")
    assert "quality" not in captured["kwargs"]


def test_input_fidelity_is_never_sent(monkeypatch, captured):
    """gpt-image-2 fails the whole request if input_fidelity is present."""
    _with_model(monkeypatch, "gpt-image-2")
    _run(reference_asset_paths=["/tmp/a.png"])
    assert "input_fidelity" not in captured["kwargs"]


def test_transparent_background_is_dropped_for_gpt_image_2(monkeypatch, captured):
    _with_model(monkeypatch, "gpt-image-2")
    _run(background="transparent")
    assert "background" not in captured["kwargs"]


def test_transparent_background_is_kept_for_gpt_image_1(monkeypatch, captured):
    _with_model(monkeypatch, "gpt-image-1")
    _run(background="transparent")
    assert captured["kwargs"]["background"] == "transparent"


def test_opaque_background_is_always_allowed(monkeypatch, captured):
    _with_model(monkeypatch, "gpt-image-2")
    _run(background="opaque")
    assert captured["kwargs"]["background"] == "opaque"


# --- reference images ----------------------------------------------------------------- #

def test_no_references_uses_the_generate_endpoint(monkeypatch, captured):
    _with_model(monkeypatch, "gpt-image-2")
    _run()
    assert captured["endpoint"] == "generate"


def test_references_switch_to_the_edit_endpoint(monkeypatch, captured):
    """This is what keeps a character or product consistent across posts."""
    _with_model(monkeypatch, "gpt-image-2")
    _run(reference_asset_paths=["/tmp/a.png", "/tmp/b.png"])
    assert captured["endpoint"] == "edit"
    assert len(captured["kwargs"]["image"]) == 2


def test_reference_count_is_capped_at_sixteen(monkeypatch, captured):
    _with_model(monkeypatch, "gpt-image-2")
    _run(reference_asset_paths=[f"/tmp/{i}.png" for i in range(30)])
    assert len(captured["kwargs"]["image"]) == image_tool.MAX_REFERENCE_IMAGES


def test_empty_reference_paths_are_ignored(monkeypatch, captured):
    _with_model(monkeypatch, "gpt-image-2")
    _run(reference_asset_paths=["", None])
    assert captured["endpoint"] == "generate"


# --- results and failures -------------------------------------------------------------- #

def test_result_records_the_asset_on_state(monkeypatch, captured):
    _with_model(monkeypatch, "gpt-image-2")
    state = {}
    result = _run(state, output_format="jpeg")
    assert result["asset_path"].endswith(".jpg")
    assert state["assets"][0]["kind"] == "image"


def test_adjustment_is_reported_back_to_the_agent(monkeypatch, captured):
    _with_model(monkeypatch, "gpt-image-2")
    result = _run(size="1441x1801")
    assert "multiple of 16" in result["adjustment"]


def test_api_failure_is_reported_not_raised(monkeypatch, tmp_path):
    _with_model(monkeypatch, "gpt-image-2")

    class Boom:
        class images:
            @staticmethod
            async def generate(**kwargs):
                raise RuntimeError("content filtered")

    monkeypatch.setattr(image_tool, "_client", lambda: Boom())
    state = {}
    result = _run(state)
    assert result["error"] == "image_generation_failed"
    assert state["image_failures"] == 1
