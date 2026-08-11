"""``describe_image`` — the agent's eyes.

Everything else in the run is text. ``browse_page`` hands back a URL, alt text
and the surrounding caption; ``generate_image`` hands back a path. The agent has
never actually *seen* any of it, so a page whose meaning is in the picture — a
comic panel, a chart, a screenshot, four frames that must be put in order — left
it guessing from filenames.

This tool takes either a saved ``asset_path`` or a public image URL, and asks a
small vision agent (:mod:`aismm.agent.vision`) what is in it. Use it *when
needed*, not on everything: it is a model call, and the alt text is often
enough.

The deterministic work lives here — resolving the target, fetching bytes,
rejecting non-images, shrinking something enormous — and only the looking is
delegated, keeping the "tools do deterministic work only" convention as intact
as a vision tool can.
"""
from __future__ import annotations

import logging

import httpx
from agents import function_tool

from .. import media
from ..assets import exists as asset_exists
from ..assets import read_bytes
from ..config import settings
from .browse_tool import is_public_url, sniff_media
from .registry import register_tool

logger = logging.getLogger("aismm.tools.vision")

# Above this the image is downscaled before it is sent. A 20MB screenshot is
# nothing but upload time — the model does not see the extra pixels.
_MAX_SEND_BYTES = 8 * 1024 * 1024
_MAX_SEND_WIDTH = 2000
# What we are willing to pull off the network at all.
_MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024

_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "gif": "image/gif", "webp": "image/webp"}


async def _fetch(target: str) -> tuple[bytes, str, str]:
    """Return ``(data, mime, error)`` for a URL or a saved asset path."""
    if target.lower().startswith(("http://", "https://")):
        ok, why = is_public_url(target)
        if not ok:
            return b"", "", why
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(target, headers={"User-Agent": "Mozilla/5.0 (AISMM)"})
                resp.raise_for_status()
                data = resp.content
                declared = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        except Exception as exc:  # noqa: BLE001
            return b"", "", f"Could not download {target}: {type(exc).__name__}: {exc}"
    else:
        if not asset_exists(target):
            return b"", "", (f"No asset at {target}. Use the asset_path returned by save_media "
                             f"or generate_image.")
        try:
            data = read_bytes(target)
        except Exception as exc:  # noqa: BLE001
            return b"", "", f"Could not read {target}: {exc}"
        declared = ""

    if len(data) > _MAX_DOWNLOAD_BYTES:
        return b"", "", f"{len(data)} bytes is too large to look at."

    kind, ext, how = sniff_media(data, declared, target)
    if kind == "video":
        return b"", "", ("That is a video, and this tool only looks at images. Describe it from "
                         "the plan you generated it with, or look at a still instead.")
    if kind != "image":
        return b"", "", f"{target} is not an image — {how}."
    return data, _MIME.get(ext, "image/jpeg"), ""


async def perform_describe_image(target: str, question: str = "", *, model=None) -> dict:
    """Fetch, validate and describe one image (extracted for testability).

    ``model`` reuses the run's LLM connection when the caller passes one; the
    describer falls back to the deployment default otherwise.
    """
    target = (target or "").strip()
    if not target:
        return {"error": "no_target",
                "message": "Pass an asset_path or a public image URL."}

    data, mime, error = await _fetch(target)
    if error:
        logger.warning("describe_image refused %s: %s", target, error)
        return {"error": "cannot_read", "message": error}

    sent_bytes = len(data)
    if sent_bytes > _MAX_SEND_BYTES:
        try:
            data = media.normalize_image(data, max_bytes=_MAX_SEND_BYTES,
                                         max_width=_MAX_SEND_WIDTH)
            mime = "image/jpeg"
            sent_bytes = len(data)
        except Exception as exc:  # noqa: BLE001 - a failed shrink is not fatal
            logger.warning("Could not shrink %s before describing it: %s", target, exc)

    from ..agent.vision import describe_image as _describe   # lazy: keeps tools import-light

    try:
        description = await _describe(data, mime=mime, question=question, source=target,
                                      model=model)
    except Exception as exc:  # noqa: BLE001
        logger.warning("describe_image failed for %s: %s", target, exc)
        return {"error": "vision_failed",
                "message": (f"Could not describe the image: {type(exc).__name__}: {exc}. "
                            f"Carry on without it, or report_failure if you cannot.")}

    if not description:
        return {"error": "vision_failed",
                "message": "The model returned no description for that image."}
    logger.info("Described %s in %d chars", target, len(description))
    return {"description": description, "source": target, "bytes": sent_bytes}


def _make_describe_image(state: dict):
    # Nothing to call if the LLM is unconfigured — the run could not have started,
    # but a tool that would always fail should not be offered.
    if not (settings.llm.azure_api_key or settings.llm.apim_subscription_key):
        return None

    @function_tool
    async def describe_image(asset_path_or_url: str, question: str = "") -> dict:
        """LOOK at an image and get back a description of what is in it.

        You cannot see images by default — ``browse_page`` gives you a URL and
        alt text, not the picture. Call this to UNDERSTAND an image you did not
        make: which panel shows what, roughly what a speech bubble says, whether
        a chart or a photo is actually there, or which of several images to post.

        ``asset_path_or_url`` is either an ``asset_path`` from ``save_media`` /
        ``generate_image``, or a public image URL from ``browse_page``.
        ``question`` narrows the description — "what does the sign say?", "is the
        character holding a letter?" — and is far more useful than a generic
        look. Images only; a video cannot be described this way.

        **Do not use this to proof-read an image you generated.** It reads text
        approximately, and it is least reliable on exactly the things worth
        checking: phone numbers, non-Latin scripts, right-to-left text, small
        print. It has reported a correct phone number as malformed and a correct
        Persian footer as garbled — a false alarm here throws away a good image,
        burns another generation, and can fail the whole run. If exact rendered
        wording matters, keep it out of the image and put it in the caption, or
        stage the post for a human to look at. Judge your own output by whether
        you asked for the right thing, not by asking this whether it arrived.

        This costs a model call, so use it when the surrounding text is not
        enough, not on every image you come across.
        """
        return await perform_describe_image(asset_path_or_url, question,
                                            model=state.get("model"))

    return describe_image


register_tool("describe_image", _make_describe_image)
