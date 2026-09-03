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
    assert result["reference_images_used"] == 1


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
    assert "reference_images_used" not in result


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


# --- per-shot direction: length, cut-or-continue, and one image each ------------------- #
# Reported after a trailer run: "lots of gaps and repeats between videos", every
# clip 4 seconds, and one seed image for the whole sequence — in a shot where the
# character's face wasn't visible, Sora invented a different person entirely.

@pytest.fixture()
def seq(monkeypatch, tmp_path):
    """Record every Sora call a sequence makes, without touching the API."""
    calls = {"creates": [], "remixes": []}

    async def failover(prompt, seconds, size, *, ref_image_bytes=None, **kw):
        calls["creates"].append({"prompt": prompt, "seconds": seconds,
                                 "reference": ref_image_bytes})
        return b"clip", f"job-{len(calls['creates'])}", {"endpoint": "https://e"}

    async def create(resource, prompt, seconds, size, reference=None):
        calls["creates"].append({"prompt": prompt, "seconds": seconds,
                                 "reference": reference})
        return b"clip", f"job-{len(calls['creates'])}"

    async def remix(resource, base_job_id, prompt):
        calls["remixes"].append({"base": base_job_id, "prompt": prompt})
        return b"remixed", f"remix-{len(calls['remixes'])}"

    monkeypatch.setattr(sequence_tool, "create_clip_with_failover", failover)
    monkeypatch.setattr(sequence_tool, "create_clip", create)
    monkeypatch.setattr(sequence_tool, "remix_clip", remix)
    monkeypatch.setattr(sequence_tool.video, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(sequence_tool.video, "concat_clips", lambda clips, size: b"merged")
    monkeypatch.setattr(sequence_tool.video, "duration_seconds", lambda data: 8.0)
    monkeypatch.setattr(sequence_tool.video, "extract_last_frame", lambda c, s: b"tail-frame")
    monkeypatch.setattr(sequence_tool, "save_bytes", lambda data, ext: "/assets/seq.mp4")
    monkeypatch.setattr(sequence_tool, "public_url", lambda p: "https://host/seq.mp4")
    return calls


def _panels(monkeypatch, tmp_path, count):
    """`count` distinct images on disk, each a different colour."""
    paths = []
    for index in range(count):
        path = tmp_path / f"panel{index}.jpg"
        path.write_bytes(_jpeg(colour=(10 * index + 5, 40, 90)))
        paths.append(str(path))
    from aismm import assets

    monkeypatch.setattr(assets, "exists", lambda p: str(p) in paths)
    monkeypatch.setattr(assets, "read_bytes", lambda p: open(p, "rb").read())
    return paths


def _run_sequence(scenes, **kwargs):
    return asyncio.run(sequence_tool.perform_create_sequence({}, scenes, **kwargs))


def test_each_shot_gets_its_own_image(seq, monkeypatch, tmp_path):
    """The fix for one seed doing the work of a whole sequence."""
    paths = _panels(monkeypatch, tmp_path, 3)
    result = _run_sequence(["a", "b", "c"], reference_asset_paths=paths)
    references = [call["reference"] for call in seq["creates"]]
    assert all(ref is not None for ref in references)
    assert len(set(references)) == 3            # three DIFFERENT pictures
    assert result["reference_images_used"] == 3
    assert result["reference_images_given"] == 3


def test_a_shot_with_no_image_falls_back_to_the_chained_frame(seq, monkeypatch, tmp_path):
    paths = _panels(monkeypatch, tmp_path, 1)
    _run_sequence(["a", "b"], reference_asset_paths=[paths[0], ""], continuity="auto")
    assert seq["creates"][0]["reference"] is not None
    assert seq["creates"][1]["reference"] == b"tail-frame"


def test_the_supplied_image_wins_over_the_chained_frame(seq, monkeypatch, tmp_path):
    """Naming a panel for a shot is a more specific instruction than "continue"."""
    paths = _panels(monkeypatch, tmp_path, 2)
    _run_sequence(["a", "b"], reference_asset_paths=paths, continuity="auto")
    assert seq["creates"][1]["reference"] != b"tail-frame"


def test_a_shot_with_an_image_is_told_it_is_a_look_not_a_paused_video(seq, monkeypatch,
                                                                     tmp_path):
    paths = _panels(monkeypatch, tmp_path, 1)
    _run_sequence(["a"], reference_asset_paths=paths)
    prompt = seq["creates"][0]["prompt"]
    assert "SOURCE IMAGE" in prompt
    assert "not a frame to resume" in prompt


# --- cuts -------------------------------------------------------------------------- #
# A cut is TWO decisions, not one: what the prompt asks for (a new moment) and
# where the pixels come from (an earlier clip). Treating "cut" as "no source" is
# what let cuts drift into a different film — the cast changed across every jump.

def test_a_cut_is_still_remixed_from_an_earlier_clip(seq):
    """The source fixes the look; the prompt asks for a new moment."""
    _run_sequence(["a", "b"], scene_continuity=["", "cut"])
    assert len(seq["remixes"]) == 1


def test_a_cut_is_told_the_source_is_only_there_to_fix_the_look(seq):
    _run_sequence(["a", "b"], scene_continuity=["", "cut"])
    prompt = seq["remixes"][0]["prompt"]
    assert "CUT:" in prompt
    assert "ONLY to fix the look" in prompt
    assert "Do NOT continue or repeat that shot" in prompt


def test_a_cut_with_nothing_to_remix_still_says_it_is_a_new_shot(seq):
    """continuity="none" has no source, so the style block is all there is."""
    _run_sequence(["a", "b"], continuity="none", scene_continuity=["", "cut"])
    prompt = seq["creates"][1]["prompt"]
    assert "CUT:" in prompt
    assert "do NOT continue the previous shot" in prompt


def test_a_cut_still_carries_the_style(seq):
    _run_sequence(["a", "b"], style="ink and wash, teal palette",
                  scene_continuity=["", "cut"])
    assert "ink and wash, teal palette" in seq["remixes"][0]["prompt"]


def test_shots_without_a_per_shot_mode_take_the_sequence_default(seq):
    _run_sequence(["a", "b"], scene_continuity=["", ""], continuity="auto")
    assert seq["creates"][1]["reference"] == b"tail-frame"


# --- choosing WHICH shot to remix from ----------------------------------------------- #
# Chaining forward advances the action but drifts a little further with every
# link. A shot that returns to the opening framing should go back to that shot.

def test_a_shot_remixes_the_one_before_it_by_default(seq):
    _run_sequence(["a", "b", "c"])
    assert [r["base"] for r in seq["remixes"]] == ["job-1", "remix-1"]


def test_a_shot_can_remix_a_named_earlier_shot(seq):
    """[0, 0, 1] — shot 3 returns to the opening, not to shot 2."""
    _run_sequence(["a", "b", "c"], scene_remix_from=[0, 0, 1])
    assert [r["base"] for r in seq["remixes"]] == ["job-1", "job-1"]


def test_the_prompt_names_the_shot_being_edited(seq):
    _run_sequence(["a", "b", "c"], scene_remix_from=[0, 0, 1])
    assert "shot 1 of this sequence" in seq["remixes"][1]["prompt"]


def test_an_impossible_source_falls_back_instead_of_failing_the_shot(seq):
    """Forward references and failed shots must not cost a clip."""
    result = _run_sequence(["a", "b", "c"], scene_remix_from=[0, 3, 0])
    assert len(seq["remixes"]) == 2
    assert "not available" in " ".join(result["timing_notes"])


# --- length ------------------------------------------------------------------------ #

def test_clip_length_defaults_to_the_longest(seq):
    """Many 4s clips read as a slideshow; the agent had been picking them."""
    _run_sequence(["a", "b"], continuity="none")
    assert [call["seconds"] for call in seq["creates"]] == [12, 12]


def test_length_can_vary_on_shots_that_are_not_remixed(seq):
    """A remix inherits its source's duration, so varying lengths only reach
    Sora when the shots are created rather than chained."""
    _run_sequence(["a", "b", "c"], scene_seconds=[12, 4, 8], continuity="none")
    assert [call["seconds"] for call in seq["creates"]] == [12, 4, 8]


def test_varying_lengths_under_remix_are_called_out_up_front(seq):
    """Rather than letting the agent discover it per shot and caption a total
    the file does not have."""
    result = _run_sequence(["a", "b"], scene_seconds=[12, 4], continuity="remix")
    assert "inherits its source clip's duration" in " ".join(result["timing_notes"])


def test_an_odd_length_is_snapped_to_what_sora_renders(seq):
    _run_sequence(["a"], scene_seconds=[7])
    assert seq["creates"][0]["seconds"] in (4, 8, 12)


# --- a refused image falls back to continuity, never to nothing --------------------- #
# Reported from a live trailer: seven shots, each with its own panel, and Sora
# refused the panel on shots 2 and 6. Those two were re-rendered from the prompt
# alone — no image, no chain, no anchor of any kind — and they were exactly the
# two whose cast changed. The panel is out of play either way once it is refused;
# the only thing still to decide is what the shot is tied to.

@pytest.fixture()
def refuse_one(monkeypatch, tmp_path):
    """Refuse the reference on shot 2 only, and record every call."""
    calls = {"creates": [], "remixes": []}
    state = {"remix_fails": False}

    async def failover(prompt, seconds, size, *, ref_image_bytes=None, **kw):
        calls["creates"].append(ref_image_bytes)
        return b"clip", f"job-{len(calls['creates'])}", {"endpoint": "https://e"}

    async def create(resource, prompt, seconds, size, reference=None):
        calls["creates"].append(reference)
        if reference is not None and len(calls["creates"]) == 2:
            raise RuntimeError("input_reference contains a human face")
        return b"clip", f"job-{len(calls['creates'])}"

    async def remix(resource, base, prompt):
        calls["remixes"].append(base)
        if state["remix_fails"]:
            raise RuntimeError("remix unavailable")
        return b"remixed", f"remix-{len(calls['remixes'])}"

    monkeypatch.setattr(sequence_tool, "create_clip_with_failover", failover)
    monkeypatch.setattr(sequence_tool, "create_clip", create)
    monkeypatch.setattr(sequence_tool, "remix_clip", remix)
    monkeypatch.setattr(sequence_tool.video, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(sequence_tool.video, "concat_clips", lambda clips, size: b"merged")
    monkeypatch.setattr(sequence_tool.video, "duration_seconds", lambda data: 8.0)
    monkeypatch.setattr(sequence_tool.video, "extract_last_frame", lambda c, s: b"tail")
    monkeypatch.setattr(sequence_tool, "save_bytes", lambda data, ext: "/assets/seq.mp4")
    monkeypatch.setattr(sequence_tool, "public_url", lambda p: "https://host/seq.mp4")
    calls["state"] = state
    return calls


def test_a_refused_image_falls_back_to_remixing_the_sequence(refuse_one, monkeypatch,
                                                             tmp_path):
    """Rung 1, and the fix for the reported bug: an earlier clip of this same
    sequence carries the cast the refused panel was chosen to supply."""
    paths = _panels(monkeypatch, tmp_path, 2)
    result = _run_sequence(["a", "b"], reference_asset_paths=paths)
    assert refuse_one["remixes"] == ["job-1"]
    assert result["shots"][1]["how"] == "remix(shot 1, image refused)"


def test_the_shot_that_lost_its_image_is_not_counted_as_using_one(refuse_one,
                                                                  monkeypatch, tmp_path):
    paths = _panels(monkeypatch, tmp_path, 2)
    result = _run_sequence(["a", "b"], reference_asset_paths=paths)
    assert result["reference_images_used"] == 1
    assert result["reference_images_given"] == 2
    assert any("rejects images containing human faces" in note
               for note in result["reference_notes"])
    assert any("describe the character IN `style`" in note
               for note in result["reference_notes"])


def test_a_refused_image_on_shot_one_reuses_no_picture_it_does_not_have(refuse_one,
                                                                       monkeypatch,
                                                                       tmp_path):
    """There is nothing to fall back ON at the start, so the prompt is all there
    is — and that is exactly the case the result has to flag."""
    path = _panels(monkeypatch, tmp_path, 1)[0]

    async def refuse_first(prompt, seconds, size, *, ref_image_bytes=None, **kw):
        refuse_one["creates"].append(ref_image_bytes)
        if ref_image_bytes is not None:
            raise RuntimeError("input_reference contains a human face")
        return b"clip", "job-1", {"endpoint": "https://e"}

    monkeypatch.setattr(sequence_tool, "create_clip_with_failover", refuse_first)
    result = _run_sequence(["a", "b"], reference_asset_paths=[path, ""])
    assert result["shots"][0]["how"] == "create(image refused)"
    assert any("no earlier clip" in note for note in result["reference_notes"])


def test_a_second_refusal_borrows_a_picture_sora_already_accepted(refuse_one,
                                                                  monkeypatch, tmp_path):
    """Rung 2 — "use another photo". Only reachable when the remix itself fails,
    but a picture Sora has already taken is the last anchor available."""
    paths = _panels(monkeypatch, tmp_path, 2)
    refuse_one["state"]["remix_fails"] = True
    result = _run_sequence(["a", "b"], reference_asset_paths=paths)
    assert refuse_one["remixes"] == ["job-1"]              # tried, and failed
    assert result["shots"][1]["how"] == "create+image(shot 1's, image refused)"
    assert refuse_one["creates"][-1] is not None           # a real picture went out


def test_a_shot_with_its_own_picture_is_reported_as_unchained(seq, monkeypatch,
                                                              tmp_path):
    """The trailer planned scene_remix_from=[0,1,2,2,4,5,6] and not one shot was
    remixed, because a supplied image wins — and nothing said so."""
    paths = _panels(monkeypatch, tmp_path, 3)
    result = _run_sequence(["a", "b", "c"], reference_asset_paths=paths,
                           scene_remix_from=[0, 1, 2])
    assert seq["remixes"] == []
    assert any("NOT chained from an earlier shot" in note
               for note in result["timing_notes"])


def test_an_unreadable_image_is_named_in_the_result(seq, monkeypatch, tmp_path):
    paths = _panels(monkeypatch, tmp_path, 1)
    result = _run_sequence(["a", "b"], reference_asset_paths=[paths[0], "/gone/panel.jpg"])
    assert result["reference_images_given"] == 2
    assert any("/gone/panel.jpg" in note for note in result["reference_notes"])


def test_the_single_path_shorthand_still_works(seq, monkeypatch, tmp_path):
    paths = _panels(monkeypatch, tmp_path, 1)
    _run_sequence(["a", "b"], reference_asset_path=paths[0], continuity="auto")
    assert seq["creates"][0]["reference"] is not None
    assert seq["creates"][1]["reference"] == b"tail-frame"


# --- what the agent is told about references, after the face rule --------------------- #
# Reported: Sora blocks ANY input_reference showing a human face — including one
# gpt-image-2 has just drawn. The earlier routine (character sheet -> paint the
# opening frame of every cut -> hand those frames to the sequence) was therefore
# advice to spend money on images that are refused on arrival. Identity lives in
# `style`; continuity lives in remix.

def test_the_prompt_says_a_generated_image_is_refused_just_the_same():
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS as p

    assert "including an\n       image you just made with generate_image" in p
    assert "Who drew it makes no difference" in p


def test_the_prompt_no_longer_asks_for_a_character_sheet():
    """This is what sent runs off building sheets and painting frames."""
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS as p

    assert "do NOT build a character sheet" in p
    assert "OPENING FRAME of shot 1" not in p


def test_the_prompt_names_the_two_levers_that_do_work():
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS as p

    assert "`style`, repeated identically in every shot" in p
    assert "Every shot after the first is a REMIX" in p


def test_the_prompt_keeps_references_for_material_without_people():
    """The rule is about faces, not about references — a location still helps."""
    from aismm.agent.prompts import MANAGER_INSTRUCTIONS as p

    assert "locations, objects, artwork, landscapes" in p


def test_the_sequence_tool_says_the_same_at_call_time():
    """The agent reads the tool docstring when it calls it, not just the prompt."""
    import inspect

    source = inspect.getsource(sequence_tool)
    assert "Only for material with NO people in it" in source
    assert "do not paint\n                opening frames of people" in source


def test_the_image_tool_no_longer_advertises_painting_video_frames():
    import inspect

    from aismm.tools import image_tool

    source = inspect.getsource(image_tool)
    assert "Do NOT paint frames here to feed a video" in source
    assert "CHARACTER SHEET" not in source
