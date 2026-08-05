"""ffmpeg helpers for stitching Sora clips into one video.

Ported from ``SandBox/GenBox/newsvideo/mux.py`` and generalised: the target size
is a parameter rather than a module constant, so one deployment can produce
portrait Reels and landscape YouTube from the same code.

Uses **imageio-ffmpeg's bundled static binary**, so a deploy is still just
``pip install`` — no apt, no system ffmpeg. Everything here is blocking and CPU
bound; callers run it through ``asyncio.to_thread``.

Two details that are not optional:

* **Every clip is re-encoded to identical parameters before concatenation.** The
  concat demuxer stream-copies, so mismatched fps / sample rate / channel layout
  produces a file that plays only the first clip, or plays with no audio. Sora
  clips are consistent today, but a clip saved from the web (``save_media``) is
  not.
* **A clip with no audio track gets silence.** Concatenating a silent clip with a
  clip that has audio drops the audio stream entirely for the rest of the video.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile

logger = logging.getLogger("aismm.video")

# Uniform encode parameters so the concat demuxer can stream-copy.
FPS = 30
SAMPLE_RATE = "44100"
CHANNELS = "2"
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")


def ffmpeg_available() -> bool:
    try:
        ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return False
    return True


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        tail = (result.stderr or b"").decode("utf-8", "replace")[-600:]
        raise RuntimeError(f"ffmpeg failed: {tail}")
    return result


def _probe(path: str) -> bytes:
    """``ffmpeg -i`` with no output exits non-zero but prints stream info to stderr.

    imageio-ffmpeg ships ffmpeg but not ffprobe, so this is how we inspect a file.
    """
    return subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path],
                          capture_output=True).stderr or b""


def has_audio(path: str) -> bool:
    return b"Audio:" in _probe(path)


def duration_seconds(clip_bytes: bytes) -> float:
    """Length of a clip in seconds, or 0.0 when ffmpeg can't tell."""
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "probe.mp4")
        with open(path, "wb") as handle:
            handle.write(clip_bytes)
        match = _DURATION.search(_probe(path).decode("utf-8", "replace"))
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def extract_last_frame(clip_bytes: bytes, size: str) -> bytes:
    """The clip's final frame as PNG bytes, scaled to exactly ``size``.

    This is what makes the next clip start where this one ended. Sora requires an
    ``input_reference`` matching the requested ``size``, so the scale is not
    cosmetic — a mismatch is rejected.
    """
    width, height = (int(part) for part in size.split("x"))
    with tempfile.TemporaryDirectory() as directory:
        source = os.path.join(directory, "in.mp4")
        output = os.path.join(directory, "frame.png")
        with open(source, "wb") as handle:
            handle.write(clip_bytes)
        # Seek to the last second, then take one frame.
        _run([ffmpeg_exe(), "-y", "-sseof", "-1", "-i", source,
              "-frames:v", "1", "-vf", f"scale={width}:{height}", output])
        with open(output, "rb") as handle:
            return handle.read()


def _normalize(source: str, destination: str, size: str) -> None:
    """Re-encode one clip to uniform H.264/AAC at ``size``, adding silence if mute.

    Two things beyond the re-encode, both about clips that did not come from Sora
    (anything ``save_media`` pulled off the web or a phone):

    **Rotation is baked in, and the flag is cleared.** A portrait video shot on a
    phone is usually stored landscape with a 90° display matrix that the player
    applies. ffmpeg autorotates when filtering, so the pixels come out upright —
    but if the display matrix also survives into the output, the player rotates
    the *already upright* picture a second time, which is how merged clips ended
    up on their side. ``-metadata:s:v:0 rotate=0`` clears it on the way out.

    Do **not** "fix" this with ``-display_rotation 0`` on the input: that does not
    mean "bake it in", it means *ignore* the source's rotation, so a phone video
    stays sideways. Measured: with that flag a 360x640 clip flagged 90° filled a
    720x1280 frame (i.e. was never rotated); with plain autorotation it became
    640x360 letterboxed, which is correct.

    **Fit and pad, never stretch.** ``scale=w:h`` alone squashes a 9:16 clip into
    a 16:9 frame. Letterboxing keeps every source's geometry, which matters as
    soon as one sequence mixes a saved post with a generated clip.
    """
    width, height = (int(part) for part in size.split("x"))
    video_filter = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                    f"setsar=1,fps={FPS}")
    source_args = ["-i", source]      # autorotation is ffmpeg's default; keep it
    common = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
              "-metadata:s:v:0", "rotate=0",
              "-c:a", "aac", "-ar", SAMPLE_RATE, "-ac", CHANNELS, destination]
    if has_audio(source):
        _run([ffmpeg_exe(), "-y", *source_args, "-vf", video_filter, *common])
    else:
        _run([ffmpeg_exe(), "-y", *source_args,
              "-f", "lavfi", "-i",
              f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}",
              "-vf", video_filter, "-map", "0:v:0", "-map", "1:a:0", "-shortest",
              *common])


def concat_clips(clips: list[bytes], size: str) -> bytes:
    """Normalize then concatenate clips into one MP4. Returns the merged bytes."""
    if not clips:
        raise ValueError("concat_clips: no clips provided")
    with tempfile.TemporaryDirectory() as directory:
        normalized = []
        for index, clip in enumerate(clips):
            source = os.path.join(directory, f"src{index}.mp4")
            destination = os.path.join(directory, f"norm{index}.mp4")
            with open(source, "wb") as handle:
                handle.write(clip)
            _normalize(source, destination, size)
            normalized.append(destination)

        if len(normalized) == 1:
            with open(normalized[0], "rb") as handle:
                return handle.read()

        listing = os.path.join(directory, "list.txt")
        with open(listing, "w") as handle:
            for path in normalized:
                handle.write(f"file '{path}'\n")
        merged = os.path.join(directory, "merged.mp4")
        _run([ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0", "-i", listing,
              "-c", "copy", "-movflags", "+faststart", merged])
        with open(merged, "rb") as handle:
            data = handle.read()
    logger.info("Merged %d clip(s) into %d bytes at %s", len(clips), len(data), size)
    return data
