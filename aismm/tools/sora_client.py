"""Async client for the Sora 2 video API on Azure OpenAI.

Adapted from SandBox/GenBox. The Azure video surface is a bespoke REST endpoint
(``/openai/v1/videos``) not wrapped by the OpenAI SDK ``images.*`` helpers, so we
call it directly with httpx. Workflow is job-scoped:

    create  -> POST {endpoint}/openai/v1/videos?api-version=preview        -> {id}
    poll    -> GET  {endpoint}/openai/v1/videos/{id}?api-version=preview
    content -> GET  {endpoint}/openai/v1/videos/{id}/content?api-version=preview  (MP4)
    remix   -> POST {endpoint}/openai/v1/videos/{id}/remix?api-version=preview

A returned id only exists on the resource that served the create call, so poll /
download / remix MUST target that SAME resource (see ``sora_config``). Auth is the
Azure-style ``api-key`` header.

NOTE: Sora 2's Videos API is announced for shutdown ~Sep 24 2026. This client sits
behind the tool registry so a successor video model can replace it.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from . import sora_config as config

logger = logging.getLogger("aismm.sora")

_SUCCESS_STATES = {"completed", "succeeded"}
_FAILURE_STATES = {"failed", "cancelled", "canceled"}


def _videos_url(resource: dict, suffix: str = "") -> str:
    return (
        f"{resource['endpoint'].rstrip('/')}/openai/v1/videos{suffix}"
        f"?api-version={config.api_version()}"
    )


def _headers(resource: dict) -> dict:
    return {"api-key": resource["key"]}


def _host(resource: dict) -> str:
    return resource.get("endpoint", "").split("://", 1)[-1].split("/", 1)[0]


def _multipart_parts(model, prompt, seconds, size, image_bytes=None) -> dict:
    """Build multipart/form-data parts for a create call (Sora 2 has no seed)."""
    parts = {
        "model": (None, model),
        "prompt": (None, prompt),
        "size": (None, size),
        "seconds": (None, str(seconds)),
    }
    if image_bytes is not None:
        parts["input_reference"] = ("frame.png", image_bytes, "image/png")
    return parts


async def create_video_job(resource, prompt, seconds, size, image_bytes=None) -> str:
    parts = _multipart_parts(resource["model"], prompt, seconds, size, image_bytes)
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(_videos_url(resource), headers=_headers(resource), files=parts)
        resp.raise_for_status()
        return resp.json()["id"]


async def remix_video_job(resource, base_job_id, prompt) -> str:
    """Remix inherits the source's model/size/duration; it takes ONLY a JSON prompt."""
    url = _videos_url(resource, f"/{base_job_id}/remix")
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(url, headers=_headers(resource), json={"prompt": prompt})
        resp.raise_for_status()
        return resp.json()["id"]


async def poll_until_complete(resource, job_id, interval=6.0, timeout=900.0) -> dict:
    url = _videos_url(resource, f"/{job_id}")
    waited = 0.0
    async with httpx.AsyncClient(timeout=60) as client:
        while waited < timeout:
            resp = await client.get(url, headers=_headers(resource))
            resp.raise_for_status()
            job = resp.json()
            status = (job.get("status") or "").lower()
            if status in _SUCCESS_STATES:
                return job
            if status in _FAILURE_STATES:
                raise RuntimeError(f"Sora job {job_id} {status}: {job.get('error')}")
            await asyncio.sleep(interval)
            waited += interval
    raise TimeoutError(f"Sora job {job_id} timed out after {timeout}s")


async def download_video_bytes(resource, job_id) -> bytes:
    url = _videos_url(resource, f"/{job_id}/content")
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.get(url, headers=_headers(resource))
        resp.raise_for_status()
        return resp.content


async def create_clip(resource, prompt, seconds, size, ref_image_bytes=None) -> tuple[bytes, str]:
    """create -> poll -> download on ``resource``. Returns ``(mp4_bytes, job_id)``."""
    logger.info("Sora create_clip on %s (model=%s, %ss, %s)",
                _host(resource), resource.get("model"), seconds, size)
    job_id = await create_video_job(resource, prompt, seconds, size, image_bytes=ref_image_bytes)
    await poll_until_complete(resource, job_id)
    return await download_video_bytes(resource, job_id), job_id


async def generate_video_bytes(prompt: str, seconds: int, size: str, *, max_attempts: int = 2) -> bytes:
    """High-level: pick a resource and generate a clip, failing over to a different
    resource on error (e.g. one endpoint out of credits). Returns MP4 bytes.
    """
    tried: set[str] = set()
    last_exc: Exception | None = None
    for _ in range(max_attempts):
        resource = config.next_resource(exclude_endpoints=tried)
        tried.add(resource["endpoint"])
        try:
            mp4, _job = await create_clip(resource, prompt, seconds, size)
            return mp4
        except Exception as exc:  # noqa: BLE001 - failover to another resource
            logger.warning("Sora clip failed on %s: %s", _host(resource), exc)
            last_exc = exc
    raise RuntimeError(f"Sora video generation failed after {max_attempts} attempt(s): {last_exc}")
