"""``generate_image`` — Azure image generation (gpt-image-1 / DALL·E) as a tool.

Uses a separate Azure resource (the ``AZURE_OPENAI_*_DALLE`` split, mirroring
ComicBook's ``getimage.py``). Disables itself when the image resource is unset.
"""
from __future__ import annotations

import base64
import logging
from functools import lru_cache

from agents import function_tool
from openai import AsyncAzureOpenAI

from ..assets import public_url, save_bytes
from ..config import settings
from .registry import register_tool

logger = logging.getLogger("aismm.tools.image")

# Portrait/landscape/square sizes valid for gpt-image-1.
_SIZES = {"portrait": "1024x1536", "landscape": "1536x1024", "square": "1024x1024"}


@lru_cache(maxsize=1)
def _client() -> AsyncAzureOpenAI:
    img = settings.image
    return AsyncAzureOpenAI(
        api_key=img.api_key,
        api_version=img.api_version,
        azure_endpoint=img.endpoint,
        timeout=600.0,
    )


def _make_generate_image(state: dict):
    if not settings.image.enabled:
        return None

    @function_tool
    async def generate_image(prompt: str, orientation: str = "square") -> dict:
        """Generate a still image for the post.

        Args:
            prompt: Description of the image to create.
            orientation: "portrait", "landscape", or "square".

        Returns a dict with ``asset_path`` and ``public_url`` to pass to ``publish``.
        """
        size = _SIZES.get(orientation.lower(), _SIZES["square"])
        try:
            resp = await _client().images.generate(
                model=settings.image.model, prompt=prompt, size=size, n=1,
            )
            b64 = resp.data[0].b64_json
            data = base64.b64decode(b64)
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_image failed: %s", exc)
            return {"error": "image_generation_failed", "message": str(exc)}
        path = save_bytes(data, "png")
        asset = {"path": path, "kind": "image", "public_url": public_url(path), "size": size}
        state.setdefault("assets", []).append(asset)
        return {"asset_path": path, "public_url": asset["public_url"], "kind": "image", "size": size}

    return generate_image


register_tool("generate_image", _make_generate_image)
