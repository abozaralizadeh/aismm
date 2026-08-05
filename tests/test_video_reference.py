"""Building a video FROM an image, and merging clips without turning them sideways.

Reported after a reel run: asked to use the account's own photos as reference
images, the agent described them with ``describe_image`` and put the prose in the
prompt instead. It had no choice — neither video tool accepted an image, so the
only way to "use" one was to talk about it, which throws away everything the
picture actually shows.

Also here: the merge must not rotate a clip, and must not stretch one.
"""
import asyncio
import io
import os
import subprocess

import pytest
from PIL import Image

from aismm import media, video
from aismm.tools import sequence_tool, sora_client, video_tool


def _jpeg(size=(1080, 1080), colour=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG")
    return buffer.getvalue()


# --- fitting an arbitrary image to the clip size ------------------------------------- #
# Sora rejects an input_reference whose dimensions don't match the clip, so a
# photo off a post has to be fitted first. The agent should not have to know that.

@pytest.mark.parametrize("source", [(1080, 1080), (1920, 1080), (600, 1500), (40, 40)])
def test_any_shape_becomes_exactly_the_clip_size(source):
    out = media.fit_reference(_jpeg(source), "720x1280")
    image = Image.open(io.BytesIO(out))
    assert image.size == (720, 1280)
    assert image.format == "PNG"


def test_a_reference_is_padded_never_stretched():
    """A squashed character sheet is a worse reference than a letterboxed one."""
    out = media.fit_reference(_jpeg((1000, 500)), "720x1280")
    image = Image.open(io.BytesIO(out)).convert("RGB")
    assert image.getpixel((360, 5)) == (0, 0, 0)              # padded above
    assert image.getpixel((360, 640)) != (0, 0, 0)            # content in the middle


def test_loading_a_reference_reports_why_it_cannot_be_used():
    data, note = sora_client.load_reference_image("/nope/missing.png", "720x1280")
    assert data is None
    assert "save_media" in note


def test_a_video_is_not_a_reference(monkeypatch, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
    from aismm import assets

    monkeypatch.setattr(assets, "exists", lambda p: True)
    monkeypatch.setattr(assets, "read_bytes", lambda p: path.read_bytes())
    data, note = sora_client.load_reference_image(str(path), "720x1280")
    assert data is None and "image" in note


def test_no_reference_asked_for_is_not_an_error():
    assert sora_client.load_reference_image("", "720x1280") == (None, "")


# --- generate_video takes an image ---------------------------------------------------- #

@pytest.fixture()
def sora(monkeypatch, tmp_path):
    """Capture what is sent to Sora; never call it."""
    seen = {}

    async def fake_create(prompt, seconds, size, *, ref_image_bytes=None, **kw):
        seen.setdefault("calls", []).append(
            {"prompt": prompt, "seconds": seconds, "size": size,
             "reference_bytes": len(ref_image_bytes) if ref_image_bytes else 0})
        return b"\x00\x00\x00\x18ftypmp42clip", "job-1", {"endpoint": "https://e"}

    monkeypatch.setattr(video_tool, "create_clip_with_failover", fake_create)
    monkeypatch.setattr(video_tool, "save_bytes", lambda data, ext: f"/assets/x.{ext}")
    monkeypatch.setattr(video_tool, "public_url", lambda p: f"https://host{p}")
    return seen


def _reference_on_disk(monkeypatch, tmp_path, data=None):
    path = tmp_path / "panel.jpg"
    path.write_bytes(data if data is not None else _jpeg())
    from aismm import assets

    monkeypatch.setattr(assets, "exists", lambda p: str(p) == str(path))
    monkeypatch.setattr(assets, "read_bytes", lambda p: path.read_bytes())
    return str(path)


def _generate(state, prompt="a clip", **kwargs):
    return asyncio.run(video_tool.perform_generate_video(state, prompt, **kwargs))


def test_the_real_image_reaches_sora(sora, monkeypatch, tmp_path):
    """Not a description of it — the bytes."""
    path = _reference_on_disk(monkeypatch, tmp_path)
    result = _generate({}, prompt="a panel comes alive", reference_asset_path=path)
    call = sora["calls"][0]
    assert call["reference_bytes"] > 0
    assert result["reference_used"] is True


def test_the_reference_is_sized_to_the_clip(sora, monkeypatch, tmp_path):
    path = _reference_on_disk(monkeypatch, tmp_path, _jpeg((1920, 1080)))
    _generate({}, prompt="x", orientation="portrait", reference_asset_path=path)
    assert sora["calls"][0]["reference_bytes"] > 0     # accepted, i.e. it was fitted


def test_without_a_reference_nothing_changes(sora):
    result = _generate({}, prompt="a clip")
    assert sora["calls"][0]["reference_bytes"] == 0
    assert "reference_used" not in result             # only reported when asked for


def test_a_refused_reference_still_produces_a_clip(monkeypatch, tmp_path):
    """Sora rejects images with human faces. Losing the clip over that is worse
    than losing the reference — but the agent must be told."""
    path = _reference_on_disk(monkeypatch, tmp_path)
    attempts = []

    async def fake_create(prompt, seconds, size, *, ref_image_bytes=None, **kw):
        attempts.append(bool(ref_image_bytes))
        if ref_image_bytes:
            raise RuntimeError("input_reference contains a human face")
        return b"clip", "job-2", {"endpoint": "https://e"}

    monkeypatch.setattr(video_tool, "create_clip_with_failover", fake_create)
    monkeypatch.setattr(video_tool, "save_bytes", lambda data, ext: "/assets/x.mp4")
    monkeypatch.setattr(video_tool, "public_url", lambda p: "https://host/x.mp4")

    result = _generate({}, prompt="x", reference_asset_path=path)
    assert attempts == [True, False]                  # tried with, then without
    assert result["asset_path"] == "/assets/x.mp4"
    assert result["reference_used"] is False
    assert "refused" in result["reference_note"]


def test_a_missing_reference_does_not_stop_the_clip(sora, monkeypatch):
    from aismm import assets

    monkeypatch.setattr(assets, "exists", lambda p: False)
    result = _generate({}, prompt="x", reference_asset_path="/gone.jpg")
    assert result["asset_path"]
    assert result["reference_used"] is False


# --- create_video_sequence seeds shot 1 from the image -------------------------------- #

def test_a_sequence_can_be_seeded_from_an_image(monkeypatch, tmp_path):
    path = _reference_on_disk(monkeypatch, tmp_path)
    calls = []

    async def fake_failover(prompt, seconds, size, *, ref_image_bytes=None, **kw):
        calls.append(bool(ref_image_bytes))
        return b"clip", "job-1", {"endpoint": "https://e"}

    async def fake_create(resource, prompt, seconds, size, reference=None):
        calls.append(bool(reference))
        return b"clip", f"job-{len(calls)}"

    monkeypatch.setattr(sequence_tool, "create_clip_with_failover", fake_failover)
    monkeypatch.setattr(sequence_tool, "create_clip", fake_create)
    monkeypatch.setattr(sequence_tool.video, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(sequence_tool.video, "concat_clips", lambda clips, size: b"merged")
    monkeypatch.setattr(sequence_tool.video, "duration_seconds", lambda data: 8.0)
    monkeypatch.setattr(sequence_tool.video, "extract_last_frame", lambda clip, size: b"frame")
    monkeypatch.setattr(sequence_tool, "save_bytes", lambda data, ext: "/assets/seq.mp4")
    monkeypatch.setattr(sequence_tool, "public_url", lambda p: "https://host/seq.mp4")

    result = asyncio.run(sequence_tool.perform_create_sequence(
        {}, ["shot one", "shot two"], style="ink", reference_asset_path=path))
    assert calls[0] is True                    # shot 1 got the supplied image
    assert result["reference_used"] is True


def test_a_sequence_without_a_reference_is_unchanged(monkeypatch):
    calls = []

    async def fake_failover(prompt, seconds, size, *, ref_image_bytes=None, **kw):
        calls.append(bool(ref_image_bytes))
        return b"clip", "job-1", {"endpoint": "https://e"}

    monkeypatch.setattr(sequence_tool, "create_clip_with_failover", fake_failover)
    monkeypatch.setattr(sequence_tool.video, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(sequence_tool.video, "concat_clips", lambda clips, size: b"merged")
    monkeypatch.setattr(sequence_tool.video, "duration_seconds", lambda data: 8.0)
    monkeypatch.setattr(sequence_tool.video, "extract_last_frame", lambda clip, size: b"frame")
    monkeypatch.setattr(sequence_tool, "save_bytes", lambda data, ext: "/assets/seq.mp4")
    monkeypatch.setattr(sequence_tool, "public_url", lambda p: "https://host/seq.mp4")

    result = asyncio.run(sequence_tool.perform_create_sequence({}, ["only shot"]))
    assert calls[0] is False
    assert "reference_used" not in result


# --- merging must not rotate or stretch ----------------------------------------------- #

pytestmark_ffmpeg = pytest.mark.skipif(not video.ffmpeg_available(),
                                       reason="imageio-ffmpeg not installed")


def _make_clip(tmp_path, name, size, rotate=None) -> str:
    path = tmp_path / name
    subprocess.run([video.ffmpeg_exe(), "-y", "-f", "lavfi", "-i",
                    f"testsrc=size={size}:rate=30:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   capture_output=True, check=True)
    if rotate:
        rotated = tmp_path / f"rot_{name}"
        subprocess.run([video.ffmpeg_exe(), "-y", "-display_rotation", str(rotate),
                        "-i", str(path), "-c", "copy", str(rotated)],
                       capture_output=True, check=True)
        return str(rotated)
    return str(path)


def _first_frame(data: bytes, tmp_path) -> Image.Image:
    clip = tmp_path / "merged.mp4"
    clip.write_bytes(data)
    frame = tmp_path / "frame.png"
    subprocess.run([video.ffmpeg_exe(), "-y", "-i", str(clip), "-frames:v", "1", str(frame)],
                   capture_output=True, check=True)
    return Image.open(frame).convert("RGB")


@pytestmark_ffmpeg
def test_a_rotated_clip_comes_out_upright(tmp_path):
    """Reported: some merged clips were lying on their side.

    A 360x640 source flagged 90 degrees is DISPLAYED as 640x360 landscape, so in
    a 720x1280 portrait frame it must letterbox. Filling the frame would mean the
    rotation was never applied; the pixels would be sideways.
    """
    source = _make_clip(tmp_path, "p.mp4", "360x640", rotate=90)
    merged = video.concat_clips([open(source, "rb").read()], "720x1280")
    image = _first_frame(merged, tmp_path)
    width, height = image.size
    black = lambda y: all(sum(image.getpixel((x, y))) < 30 for x in range(0, width, 40))
    assert (width, height) == (720, 1280)
    assert black(5) and black(height - 6)              # letterboxed = rotated correctly
    assert not black(height // 2)


@pytestmark_ffmpeg
def test_the_merged_file_carries_no_rotation_flag(tmp_path):
    """If it survives, the player rotates the already-upright picture again."""
    source = _make_clip(tmp_path, "p.mp4", "360x640", rotate=90)
    merged = video.concat_clips([open(source, "rb").read()], "720x1280")
    path = tmp_path / "out.mp4"
    path.write_bytes(merged)
    info = subprocess.run([video.ffmpeg_exe(), "-i", str(path)],
                          capture_output=True, text=True).stderr.lower()
    assert "displaymatrix" not in info


@pytestmark_ffmpeg
def test_a_landscape_clip_is_letterboxed_not_squashed(tmp_path):
    """Mixing a saved post with a generated clip must not distort either."""
    source = _make_clip(tmp_path, "l.mp4", "640x360")
    merged = video.concat_clips([open(source, "rb").read()], "720x1280")
    image = _first_frame(merged, tmp_path)
    width, height = image.size
    black = lambda y: all(sum(image.getpixel((x, y))) < 30 for x in range(0, width, 40))
    assert black(5) and black(height - 6)


@pytestmark_ffmpeg
def test_a_clip_already_at_the_target_size_is_not_padded(tmp_path):
    """Sora clips are generated at the target size; they must be untouched."""
    source = _make_clip(tmp_path, "s.mp4", "720x1280")
    merged = video.concat_clips([open(source, "rb").read()], "720x1280")
    image = _first_frame(merged, tmp_path)
    width, height = image.size
    black = lambda y: all(sum(image.getpixel((x, y))) < 30 for x in range(0, width, 40))
    assert not black(5) and not black(height - 6)
