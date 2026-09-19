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

**Load balancing** (same scheme as SandBox/GenBox): the pool round-robins at the
JOB level — ``create_clip_with_failover`` picks one resource and runs the whole
create/poll/download lifecycle against it, retrying on a *different* resource if
that one fails. Never front these endpoints with a round-robin gateway; it would
send each call of a job to a backend that has never heard of the job id.

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


# Azure's Sora refuses an ``input_reference`` containing human faces, and says so
# in prose rather than with a code. Shared by every caller that passes one.
FACE_REJECTION_MARKERS = ("input_reference", "face", "person", "people", "human")


def looks_like_reference_rejection(detail: str) -> bool:
    low = (detail or "").lower()
    return any(marker in low for marker in FACE_REJECTION_MARKERS)


def load_reference_image(asset_path: str, size: str):
    """``(png_bytes, note)`` for an image the agent chose as a reference.

    Returns ``(None, why)`` when it cannot be used, so the caller can generate
    from the prompt alone rather than failing the whole clip. Sora requires the
    reference to match the clip's dimensions exactly, so it is letterboxed to
    ``size`` here — the agent should not have to know that.
    """
    from ..assets import exists as asset_exists
    from ..assets import read_bytes
    from .. import media

    path = (asset_path or "").strip()
    if not path:
        return None, ""
    if not asset_exists(path):
        return None, (f"No asset at {path} — pass an asset_path from save_media, "
                      f"generate_image or a reference attachment.")
    try:
        data = read_bytes(path)
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not read {path}: {exc}"
    if data[4:8] == b"ftyp" or path.lower().endswith((".mp4", ".mov", ".webm")):
        return None, "A reference must be an image, not a video."
    try:
        return media.fit_reference(data, size), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not prepare {path} as a reference: {exc}"


def format_http_error(exc: httpx.HTTPStatusError) -> str:
    """Build an actionable message from an Azure error response (status + body).

    httpx's own message stops at the status line, but Azure puts the reason a
    resource refused the job — out of quota, deployment not found, content
    filtered — in the response body. That is exactly what you need to know when
    one endpoint in the pool starts failing, so keep the body (truncated).
    """
    resp = exc.response
    if resp is None:
        return str(exc)
    try:
        body = resp.text or ""
    except Exception:  # noqa: BLE001 - body is best-effort diagnostics
        body = ""
    req = resp.request
    where = f"{req.method} {req.url}" if req is not None else ""
    return f"HTTP {resp.status_code} {resp.reason_phrase} ({where}): {body[:800]}"


# A failure no retry can fix: the pool member is MISCONFIGURED, not busy. Measured
# on this deployment's own pool after three days of failed video runs — one
# resource answered 401 (its api-key had been rotated out from under the config)
# and the other 404 "The API deployment for this resource does not exist" (valid
# key, no Sora deployment on it at all). Rotating between those two could never
# have produced a clip, and the run log only ever showed the last of the two.
def _permanent_reason(status: int | None, body: str, resource: dict) -> str:
    """Why this resource can NEVER serve Sora, or ``""`` if the failure may pass."""
    if status in (401, 403):
        return ("its api-key is rejected — the key has been rotated, or it belongs "
                "to a different resource")
    if status == 404:
        if "deployment" in (body or "").lower():
            model = resource.get("model") or "sora"
            return (f"it has no {model!r} deployment — deploy that model there, or "
                    f"point this entry at the deployment name it does have")
        return "it does not serve the Sora videos API at this endpoint"
    return ""


def _response_body(resp) -> str:
    try:
        return resp.text or ""
    except Exception:  # noqa: BLE001 - body is best-effort diagnostics
        return ""


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


async def poll_until_complete(resource, job_id, interval=6.0, timeout=None) -> dict:
    timeout = config.job_timeout_seconds() if timeout is None else timeout
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


async def remix_clip(resource, base_job_id, prompt) -> tuple[bytes, str]:
    """remix -> poll -> download on the base clip's OWN resource.

    Remix is the strongest consistency lever Sora 2 offers: it reuses the source
    video's layout, subject and lighting and applies only what the prompt
    changes. It is job-scoped, so ``resource`` MUST be the one that served the
    base clip — a remix cannot be retried elsewhere.
    """
    logger.info("Sora remix of %s on %s", base_job_id, _host(resource))
    job_id = await remix_video_job(resource, base_job_id, prompt)
    await poll_until_complete(resource, job_id)
    return await download_video_bytes(resource, job_id), job_id


def _pool_failure_message(failures: dict[str, str], unusable: dict[str, str]) -> str:
    """Name what EVERY resource answered, not just the last one to answer.

    Reporting only the last failure is how a pool holding a rotated key on one
    resource and no Sora deployment on the other read, for three days, as a
    single "404 … deployment does not exist" — one problem instead of two, and
    the wrong one to go and fix.
    """
    lines, shown = [], []
    for resource in config.pool():
        endpoint = resource["endpoint"]
        said = failures.get(endpoint) or unusable.get(endpoint)
        if said:
            shown.append(endpoint)
            lines.append(f"  - {_host(resource)}: {said[:400]}")
    if not lines:
        return "Sora video generation failed and no resource in the pool answered."
    verdict = (
        "Every resource in the pool is misconfigured — this will fail identically "
        "on the next run, so fix the pool rather than retrying."
        if all(endpoint in unusable for endpoint in shown) else
        "At least one of these may be temporary; the rest are configuration."
    )
    return (f"Sora video generation failed on all {len(lines)} resource(s) in the "
            f"pool:\n" + "\n".join(lines) + f"\n{verdict} Check the video connection "
            "this instruction uses (or AZURE_OPENAI_ENDPOINT_SORA / _KEY_SORA / "
            "_MODEL_SORA) with: python scripts/smoke_sora.py --check")


# The last skip-set reported, so the log says it when it CHANGES rather than once
# per clip: an eight-shot sequence would otherwise repeat the same paragraph
# eight times, which is the sort of noise that hides the line that matters.
_reported_skips: set[str] = set()


def _report_skips(remembered: dict[str, str]) -> None:
    """Say which resources the pool is routing around, when that changes.

    Worth saying at all because a pool quietly skipping its dead members looks
    identical in a log to a pool that only ever had one member — which is what
    made a three-resource pool read as "load balancing isn't working".
    """
    global _reported_skips
    current = set(remembered)
    if current == _reported_skips:
        return
    _reported_skips = current
    if not current:
        logger.info("Sora pool: every resource is healthy again")
        return
    logger.info("Sora pool: %d resource(s) healthy; routing around %s",
                max(len(config.pool()) - len(current), 0),
                "; ".join(f"{e.split('://')[-1]} ({why[:90]})"
                          for e, why in remembered.items()))


async def create_clip_with_failover(
    prompt: str, seconds: int, size: str, *,
    ref_image_bytes: bytes | None = None, max_attempts: int | None = None,
    unusable: dict[str, str] | None = None,
) -> tuple[bytes, str, dict]:
    """Generate one clip, rotating to a DIFFERENT resource on each failure.

    This is the load-balancing entry point (GenBox's ``_safe_create``): each
    attempt takes the next resource round-robin while excluding the endpoints
    that already failed *this* clip, so one dead resource — out of credits (401),
    throttled (429), deployment missing (404) — can't consume every attempt.

    ``unusable`` maps endpoint -> why it can never serve Sora (see
    ``_permanent_reason``). Pass ONE dict across the shots of a sequence: a
    resource whose key is rejected or whose Sora deployment is missing is proved
    dead by the first shot, and every later shot skips it instead of paying for
    the same doomed call again. Those endpoints are still NAMED in the final
    error, so skipping one never hides it.

    That dict is now SEEDED from — and written back to — the pool-wide health
    memory in ``sora_config``, so the discovery also outlives the run that paid
    for it. A resource that has been dead for days stops being the first thing
    every new run tries; one whose key is fixed comes back by itself when the
    health TTL expires.

    Returns ``(mp4_bytes, job_id, resource)``. The serving resource comes back
    because a Sora job id only exists there: any follow-up call for this
    clip (poll, download, remix) must target that same resource.
    """
    attempts = max_attempts or config.max_attempts()
    known = unusable if unusable is not None else {}
    # NOTE what is deliberately NOT done here: the cross-run health memory is not
    # merged into `known`. `known` is a HARD skip — proved dead by this very
    # sequence — and it feeds the "every resource has already answered" guard
    # below. A remembered verdict is a GUESS about a resource nobody has called
    # this run, so folding it in here made a pool whose members were all stale-bad
    # fail without attempting a single call, and it could never climb back out.
    # The preference belongs in `config.next_resource`, which skips a remembered
    # bad member only while a better one exists and otherwise tries it anyway.
    failures: dict[str, str] = {}
    tried: set[str] = set()
    _report_skips(config.known_unusable())
    for attempt in range(attempts):
        spent = tried | set(known)
        if not [r for r in config.pool() if r["endpoint"] not in spent]:
            break   # every resource has already answered; asking again is free of hope
        resource = config.next_resource(exclude_endpoints=spent)
        tried.add(resource["endpoint"])
        try:
            mp4, job_id = await create_clip(resource, prompt, seconds, size, ref_image_bytes)
            config.mark_healthy(resource)
            if attempt:
                logger.info("Sora clip succeeded on %s after %d failed attempt(s)",
                            _host(resource), attempt)
            return mp4, job_id, resource
        except httpx.HTTPStatusError as exc:
            detail = format_http_error(exc)
            reason = _permanent_reason(exc.response.status_code if exc.response is not None
                                       else None,
                                       _response_body(exc.response), resource)
        except Exception as exc:  # noqa: BLE001 - network/timeout/etc. → try elsewhere
            detail, reason = f"{type(exc).__name__}: {exc}", ""
        failures[resource["endpoint"]] = f"{reason} ({detail})" if reason else detail
        if reason:
            known[resource["endpoint"]] = f"{reason} ({detail})"
            # Remember it for the NEXT run too, not just the rest of this one.
            config.mark_unusable(resource, f"{reason} ({detail})")
        logger.warning("Sora clip failed (attempt %d/%d on %s): %s",
                       attempt + 1, attempts, _host(resource),
                       failures[resource["endpoint"]][:400])
    raise RuntimeError(_pool_failure_message(failures, known))


# Listing the resource's DEPLOYMENTS is the only free way to tell a resource that
# can serve Sora from one that merely accepts the key. A create call cannot do it:
# it validates PARAMETERS first — measured, an invalid `seconds` answers 400 even
# on a resource with no Sora deployment at all — so the 404 that proves the
# deployment missing only arrives once the request is complete enough to bill.
# The listing lives on the OLD data-plane surface and the api-version is pinned
# for that reason: `/openai/deployments?api-version=2022-12-01` returns the list,
# while `/openai/v1/deployments?api-version=preview` and
# `/openai/deployments?api-version=2024-06-01` both answer 404.
_DEPLOYMENTS_API_VERSION = "2022-12-01"


async def check_resource(resource: dict) -> dict:
    """Can this resource actually serve Sora? Creates no job and bills nothing.

    Returns ``{endpoint, host, model, ok, detail}``. The three states this has to
    separate are the three that were live here at once: the key is refused, the
    key works but the model is not deployed, and the resource is healthy — they
    need completely different fixes and all three look the same from a run log.
    """
    out = {"endpoint": resource["endpoint"], "host": _host(resource),
           "model": resource.get("model", ""), "ok": False, "detail": ""}
    url = (f"{resource['endpoint'].rstrip('/')}/openai/deployments"
           f"?api-version={_DEPLOYMENTS_API_VERSION}")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=_headers(resource))
    except Exception as exc:  # noqa: BLE001 - a check must not raise
        out["detail"] = f"unreachable — {type(exc).__name__}: {exc}"
        return out
    reason = _permanent_reason(resp.status_code, _response_body(resp), resource)
    if resp.status_code in (401, 403):
        out["detail"] = reason
        return out
    if resp.status_code != 200:
        out["detail"] = f"HTTP {resp.status_code}: {_response_body(resp)[:200]}"
        return out
    try:
        names = sorted(d.get("id") or "" for d in (resp.json().get("data") or []))
    except Exception as exc:  # noqa: BLE001
        out["detail"] = f"could not read the deployment list: {exc}"
        return out
    if out["model"] not in names:
        out["detail"] = (f"no {out['model']!r} deployment — this resource has: "
                         f"{', '.join(n for n in names if n) or 'nothing deployed'}")
        return out
    out["ok"] = True
    out["detail"] = f"{out['model']} is deployed"
    return out


async def check_pool() -> list[dict]:
    """Run :func:`check_resource` over the active pool, concurrently."""
    return list(await asyncio.gather(*(check_resource(r) for r in config.pool())))


async def generate_video_bytes(prompt: str, seconds: int, size: str,
                               *, max_attempts: int | None = None) -> bytes:
    """Convenience wrapper over :func:`create_clip_with_failover` returning MP4 bytes."""
    mp4, _job_id, _resource = await create_clip_with_failover(
        prompt, seconds, size, max_attempts=max_attempts)
    return mp4
