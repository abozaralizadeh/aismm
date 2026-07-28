"""``publish`` — the single terminal tool. Autonomy + a code-enforced guardrail.

The agent always calls this to finish a post, but the real side effect is gated by
the instruction's ``publish_mode`` (configured per-instruction in the dashboard):

* ``dry_run``  -> save a StagedPost(preview); never touch the platform API.
* ``approval`` -> save a StagedPost(pending_approval); a human clicks Approve in
                  the dashboard, which triggers the actual publish.
* ``live``     -> publish immediately via the platform integration.

Platform / store imports are lazy to keep the tool layer decoupled.
"""
from __future__ import annotations

import logging

from agents import function_tool

from .. import disclosure, media
from ..assets import kind_from_path, read_bytes, save_bytes
from ..models import PublishMode, RunStatus, StagedPost, StagedStatus
from .registry import register_tool

logger = logging.getLogger("aismm.tools.publish")


def _normalize_image_for(asset_path: str, caps, platform_name: str) -> str:
    """Re-encode an image to what ``platform_name`` accepts; return the new path.

    Returns the original path unchanged when the platform declares no image
    constraints, or when conversion fails — a publish attempt with the original
    file gives a better error than failing here.
    """
    if not (caps.image_formats or caps.max_image_bytes or caps.min_image_ratio):
        return asset_path
    try:
        converted = media.normalize_image(
            read_bytes(asset_path),
            max_bytes=caps.max_image_bytes,
            min_ratio=caps.min_image_ratio,
            max_ratio=caps.max_image_ratio,
            max_width=caps.max_image_width,
        )
    except Exception as exc:  # noqa: BLE001 - never block a post on conversion
        logger.warning("Image conversion for %s failed (%s); using the original",
                       platform_name, exc)
        return asset_path

    new_path = save_bytes(converted, caps.image_formats[0] if caps.image_formats else "jpg")
    logger.info("Converted image for %s: %s -> %s (%d bytes)",
                platform_name, asset_path, new_path, len(converted))
    return new_path


async def perform_publish(state: dict, caption: str, asset_path: str = "", media_kind: str = "auto") -> dict:
    """Mode-gated publish logic (extracted from the tool so it is unit-testable).

    Reads ``account`` / ``instruction`` / ``store`` / ``run`` from ``state``,
    enforces the instruction's publish mode, and records the outcome on
    ``state["result"]``.
    """
    account = state["account"]
    instruction = state["instruction"]
    store = state["store"]
    run = state["run"]
    mode: PublishMode = instruction.publish_mode

    kind = media_kind if media_kind != "auto" else kind_from_path(asset_path)

    # Capability check against the target platform.
    from ..platforms.registry import get_platform  # lazy

    platform = get_platform(account.platform)
    caps = platform.capabilities
    if kind == "video" and not caps.supports_video:
        return {"error": "unsupported_media", "message": f"{account.platform.value} can't post video."}
    if kind == "image" and not caps.supports_image:
        return {"error": "unsupported_media", "message": f"{account.platform.value} can't post a standalone image."}
    if kind == "text" and not caps.supports_text:
        return {"error": "unsupported_media",
                "message": f"{account.platform.value} requires media; generate a "
                           f"{'video' if caps.supports_video else 'image'} first."}

    # Convert the image locally if this platform won't accept it as-is. Done
    # here (before staging) so the preview, the approval queue and the live post
    # all reference the SAME converted file.
    if kind == "image" and asset_path:
        asset_path = _normalize_image_for(asset_path, caps, account.platform.value)

    # AI-content disclosure, applied HERE so it reaches every path — the dry-run
    # preview and the approval queue show exactly what would be posted, and the
    # model cannot skip it. See aismm/disclosure.py for the legal/platform basis.
    caption = disclosure.apply_to_caption(caption, caption_limit=caps.caption_limit)

    run.caption = caption
    run.asset_path = asset_path
    staged = StagedPost(
        instruction_id=instruction.id, account_id=account.id, run_id=run.id,
        caption=caption, asset_path=asset_path, media_kind=kind,
    )

    # --- dry-run: preview only -------------------------------------------- #
    if mode == PublishMode.dry_run:
        staged.status = StagedStatus.preview
        store.add_staged(staged)
        run.status = RunStatus.staged
        run.log = (run.log + f"\nDRY-RUN staged {kind} post.").strip()
        store.update_run(run)
        state["result"] = {"mode": "dry_run", "staged_id": staged.id, "kind": kind}
        return {"status": "staged", "mode": "dry_run", "staged_id": staged.id,
                "message": "Prepared a dry-run preview. Nothing was published; see the dashboard."}

    # --- approval: queue for a human click -------------------------------- #
    if mode == PublishMode.approval:
        staged.status = StagedStatus.pending_approval
        store.add_staged(staged)
        run.status = RunStatus.staged
        run.log = (run.log + f"\nQueued {kind} post for approval.").strip()
        store.update_run(run)
        state["result"] = {"mode": "approval", "staged_id": staged.id, "kind": kind}
        return {"status": "pending_approval", "mode": "approval", "staged_id": staged.id,
                "message": "Queued for approval. It publishes when approved in the dashboard."}

    # --- live: publish now ------------------------------------------------ #
    try:
        access_token, _refresh = store.get_tokens(account.id)
        if not access_token:
            raise RuntimeError("account has no stored access token — reconnect it in the dashboard.")
        result = await platform.publish(
            access_token=access_token, account=account,
            caption=caption, asset_path=asset_path, media_kind=kind,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Live publish failed")
        run.status = RunStatus.failed
        run.error = str(exc)
        store.update_run(run)
        state["result"] = {"mode": "live", "error": str(exc)}
        return {"error": "publish_failed", "message": str(exc)}

    staged.status = StagedStatus.published
    staged.external_url = result.url
    store.add_staged(staged)
    run.status = RunStatus.published
    run.external_url = result.url
    run.log = (run.log + f"\nPublished {kind} post: {result.url}").strip()
    store.update_run(run)
    state["result"] = {"mode": "live", "url": result.url, "kind": kind}
    return {"status": "published", "mode": "live", "url": result.url}


def _make_publish(state: dict):
    @function_tool
    async def publish(caption: str, asset_path: str = "", media_kind: str = "auto") -> dict:
        """Publish (or stage) the finished post to the account this run targets.

        Call this exactly once, at the end, with the final caption and (optionally)
        an ``asset_path`` returned by ``generate_video`` / ``generate_image``.

        Args:
            caption: The post text / caption / title+description.
            asset_path: Path to a generated media asset, or "" for a text-only post.
            media_kind: "auto" (infer from asset), or "text"/"image"/"video".

        Returns a status dict. The publish mode (dry-run / approval / live) is set
        on the instruction and enforced here — you do not control it.
        """
        return await perform_publish(state, caption, asset_path, media_kind)

    return publish


register_tool("publish", _make_publish)
