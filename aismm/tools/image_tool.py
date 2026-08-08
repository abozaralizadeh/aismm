"""``generate_image`` / ``edit_image`` — Azure image generation as agent tools.

Uses a separate Azure resource (the ``AZURE_OPENAI_*_DALLE`` split, mirroring
ComicBook's ``getimage.py``). Disables itself when the image resource is unset.

The full option surface is exposed to the agent, because "make an image" is not
one decision: size and aspect ratio depend on the target platform, quality trades
latency against fidelity, and format matters (Instagram wants JPEG).

**Model differences that bite** — the configured deployment decides:

* ``gpt-image-2`` takes arbitrary sizes (each edge a multiple of 16, aspect ratio
  ≤ 3:1) plus 1K/2K/4K presets, always processes reference images at high
  fidelity, and **rejects ``input_fidelity`` outright** — so it is never sent.
  It also does not support transparent backgrounds.
* ``gpt-image-1`` supports transparency and ``input_fidelity``, and only a fixed
  set of sizes.

Reference images go through the **edits** endpoint (up to 16). Referring to them
as "image 1", "image 2" in the prompt is what steers which is used for what —
that guidance is in the tool docstring so the model actually does it.
"""
from __future__ import annotations

import base64
import io
import logging
from functools import lru_cache

from agents import function_tool
from openai import AsyncAzureOpenAI

from ..assets import public_url, read_bytes, save_bytes
from ..config import settings
from .registry import register_tool

logger = logging.getLogger("aismm.tools.image")

# Per-orientation defaults. gpt-image-2 accepts arbitrary sizes, but these are
# the platform-sensible ones and are valid for gpt-image-1 too.
SIZE_PRESETS = {
    "portrait": "1024x1536",
    "landscape": "1536x1024",
    "square": "1024x1024",
    # gpt-image-2 named presets
    "1k": "1024x1024",
    "2k": "2048x2048",
    "4k": "3840x2160",
    "auto": "auto",
}
QUALITIES = {"auto", "low", "medium", "high"}
FORMATS = {"png", "jpeg", "jpg", "webp"}
MAX_REFERENCE_IMAGES = 16
_MAX_RATIO = 3.0


def is_gpt_image_2(model: str) -> bool:
    return "gpt-image-2" in (model or "").lower()


def resolve_size(size: str, orientation: str, model: str) -> tuple[str, str]:
    """Return ``(size, note)`` — a size the API will accept, plus any adjustment.

    Custom sizes are validated against gpt-image-2's rules (edges a multiple of
    16, aspect ratio at most 3:1) rather than sent blind, because the API error
    for a bad size says nothing about which rule was broken.
    """
    requested = (size or "").strip().lower()
    if not requested:
        return SIZE_PRESETS.get((orientation or "square").lower(), SIZE_PRESETS["square"]), ""
    if requested in SIZE_PRESETS:
        return SIZE_PRESETS[requested], ""
    if "x" not in requested:
        return SIZE_PRESETS["square"], f"{size!r} is not a size; used 1024x1024"

    try:
        width, height = (int(part) for part in requested.split("x", 1))
    except ValueError:
        return SIZE_PRESETS["square"], f"{size!r} is not a size; used 1024x1024"

    if not is_gpt_image_2(model):
        # gpt-image-1 only accepts its fixed set.
        if requested not in {"1024x1024", "1024x1536", "1536x1024"}:
            return (SIZE_PRESETS["square"],
                    f"{model} only accepts 1024x1024 / 1024x1536 / 1536x1024; used 1024x1024")
        return requested, ""

    notes = []
    if width % 16 or height % 16:
        width, height = max(round(width / 16) * 16, 16), max(round(height / 16) * 16, 16)
        notes.append("rounded each edge to a multiple of 16")
    ratio = max(width, height) / max(min(width, height), 1)
    if ratio > _MAX_RATIO:
        if width > height:
            width = int(height * _MAX_RATIO) // 16 * 16
        else:
            height = int(width * _MAX_RATIO) // 16 * 16
        notes.append(f"clamped the aspect ratio to {_MAX_RATIO:g}:1")
    return f"{width}x{height}", "; ".join(notes)


@lru_cache(maxsize=1)
def _client() -> AsyncAzureOpenAI:
    img = settings.image
    return AsyncAzureOpenAI(
        api_key=img.api_key,
        api_version=img.api_version,
        azure_endpoint=img.endpoint,
        timeout=600.0,
    )


def _build_kwargs(*, model: str, size: str, quality: str, output_format: str,
                  background: str, compression: int | None) -> dict:
    kwargs: dict = {"model": model, "size": size, "n": 1}
    if quality in QUALITIES and quality != "auto":
        kwargs["quality"] = quality
    fmt = (output_format or "").lower().replace("jpg", "jpeg")
    if fmt in {"png", "jpeg", "webp"}:
        kwargs["output_format"] = fmt
        if compression and fmt in {"jpeg", "webp"}:
            kwargs["output_compression"] = max(1, min(int(compression), 100))
    if background in {"transparent", "opaque"}:
        if background == "transparent" and is_gpt_image_2(model):
            # gpt-image-2 rejects it; asking anyway fails the whole call.
            logger.info("Ignoring background=transparent — %s does not support it", model)
        else:
            kwargs["background"] = background
    return kwargs


async def perform_generate_image(
    state: dict, prompt: str, *, orientation: str = "square", size: str = "",
    quality: str = "auto", output_format: str = "png", background: str = "",
    compression: int | None = None, reference_asset_paths: list[str] | None = None,
) -> dict:
    """Generate (or edit, when references are given) one image. Extracted for tests."""
    model = settings.image.model
    resolved_size, note = resolve_size(size, orientation, model)
    kwargs = _build_kwargs(model=model, size=resolved_size, quality=quality,
                           output_format=output_format, background=background,
                           compression=compression)
    references = [p for p in (reference_asset_paths or []) if p][:MAX_REFERENCE_IMAGES]

    try:
        if references:
            files = []
            for index, path in enumerate(references, start=1):
                data = read_bytes(path)
                stream = io.BytesIO(data)
                stream.name = f"image{index}.png"    # the SDK needs a filename
                files.append(stream)
            logger.info("Editing with %d reference image(s): %s", len(files), kwargs)
            response = await _client().images.edit(image=files, prompt=prompt, **kwargs)
        else:
            logger.info("Generating image: %s", kwargs)
            response = await _client().images.generate(prompt=prompt, **kwargs)
        data = base64.b64decode(response.data[0].b64_json)
    except Exception as exc:  # noqa: BLE001
        state["image_failures"] = state.get("image_failures", 0) + 1
        logger.warning("generate_image failed (%s): %s", kwargs, exc)
        return {"error": "image_generation_failed", "message": str(exc)}

    ext = kwargs.get("output_format", "png")
    path = save_bytes(data, "jpg" if ext == "jpeg" else ext)
    asset = {"path": path, "kind": "image", "public_url": public_url(path),
             "size": resolved_size, "references": references}
    state.setdefault("assets", []).append(asset)
    result = {"asset_path": path, "public_url": asset["public_url"], "kind": "image",
              "size": resolved_size, "bytes": len(data)}
    if note:
        result["adjustment"] = note
    return result


def _make_generate_image(state: dict):
    if not settings.image.enabled:
        return None

    @function_tool
    async def generate_image(
        prompt: str,
        orientation: str = "square",
        size: str = "",
        quality: str = "auto",
        output_format: str = "png",
        background: str = "",
        reference_asset_paths: list[str] | None = None,
    ) -> dict:
        """Generate a still image, optionally guided by reference images.

        Args:
            prompt: What to draw. When you pass references, name them in the
                prompt — "keep the character from image 1, use the background of
                image 2" — that is how you control which reference does what.
            orientation: "portrait" (9:16-ish), "landscape", or "square". Ignored
                when ``size`` is given.
            size: Optional explicit size. Presets: "1k", "2k", "4k", or
                "WIDTHxHEIGHT" (e.g. "1440x1800"). Each edge is rounded to a
                multiple of 16 and the aspect ratio clamped to 3:1 if needed;
                the reply tells you when that happened.
            quality: "auto" (default), "low" (fast), "medium", or "high".
            output_format: "png" (default), "jpeg", or "webp". Prefer "jpeg" for
                Instagram — it only accepts JPEG, and it is converted anyway.
            background: "opaque" or "transparent". Transparency is unavailable on
                gpt-image-2 and is ignored there rather than failing the call.
            reference_asset_paths: Up to 16 asset paths (from ``save_media`` or an
                earlier ``generate_image``) to guide the result — use this to keep
                a character, product or style consistent across posts.

                Unlike video generation, this accepts references containing
                PEOPLE, so it is how you keep a character or product consistent
                across still posts — reuse the same asset_path, and record it in
                memory so the next run reuses it rather than inventing a new one.

                Do NOT paint frames here to feed a video: Sora rejects any
                reference image with a human face, whoever made it, so an image
                generated for that purpose is refused and the money is wasted.
                Video consistency comes from the sequence's own `style` block and
                continuity="remix".

        Returns ``asset_path`` and ``public_url`` to pass to ``publish``.
        """
        if state.get("image_failures", 0) >= 2:
            return {"error": "image_circuit_open",
                    "message": "Image generation failed repeatedly this run; proceed without it."}
        return await perform_generate_image(
            state, prompt, orientation=orientation, size=size, quality=quality,
            output_format=output_format, background=background,
            reference_asset_paths=reference_asset_paths)

    return generate_image


register_tool("generate_image", _make_generate_image)
