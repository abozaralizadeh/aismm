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
import re

from agents import function_tool

from .. import cooldown, disclosure, media, publish_ledger
from ..assets import exists as asset_exists
from ..assets import kind_from_path, read_bytes, save_bytes
from ..config import settings
from ..models import PublishMode, RunStatus, StagedPost, StagedStatus
from ..platforms.instagram import RateLimited
from .registry import register_tool

logger = logging.getLogger("aismm.tools.publish")

# A post that went out despite a rate-limit error still means the app is throttled.
RECONCILED_COOLDOWN_SECONDS = 3600


# A caption that narrates the agent's own trouble is a bug report, not a post —
# and it goes to real followers. The prompt tells the agent to call
# report_failure instead; this is the backstop for when it doesn't.
#
# Deliberately narrow: a failure verb must be attached to an operational object
# ("unable to retrieve"), so ordinary copy survives — "I couldn't believe the
# sunrise" does not match, and neither does news about someone else's failure.
_FAILURE_VERBS = (r"(fetch|retrieve|access|load|download|read|find|obtain|generate|"
                  r"create|produce|publish|extract)")

_META_CAPTION_PATTERNS = (
    # First person: the agent narrating its own trouble. "Rescue crews could not
    # find survivors" is news about someone else and must NOT match.
    re.compile(r"\b(i|we)\s+(was |were |am |are )?(unable to|could ?n[o']?t|failed to|"
               r"can ?n[o']?t|was not able to)\s+" + _FAILURE_VERBS + r"\b", re.IGNORECASE),
    # Subject-less, at the start of the caption or a sentence — same voice.
    re.compile(r"(^|[.!?\n]\s*)(unable to|could ?n[o']?t|failed to|was not able to)\s+"
               + _FAILURE_VERBS + r"\b", re.IGNORECASE),
    re.compile(r"\b(browse_page|save_media|generate_image|generate_video|report_failure|"
               r"read_memory|update_memory)\b", re.IGNORECASE),
    re.compile(r"\bas an ai (language )?(model|assistant)\b", re.IGNORECASE),
    re.compile(r"\bi (apolog(ise|ize)|am sorry|'m sorry)\b", re.IGNORECASE),
    re.compile(r"\b(placeholder (post|content|image)|dummy (post|content)|"
               r"technical (issue|difficulties|problem))\b", re.IGNORECASE),
)


def meta_caption_reason(caption: str) -> str:
    """Return the offending phrase if this caption is about the agent's own failure."""
    for pattern in _META_CAPTION_PATTERNS:
        match = pattern.search(caption or "")
        if match:
            return match.group(0)
    return ""


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


def _schedule_advice(instruction) -> str:
    """Name the likely cause when an instruction fires often enough to trip limits."""
    schedule = (getattr(instruction, "schedule", "") or "").lower()
    for marker in ("every 1h", "every 1 h", "every 30m", "every 15m", "every 5m",
                   "every 10m", "every 20m", "every 2h"):
        if marker in schedule:
            return (f" This instruction runs '{instruction.schedule}' — that is far more often "
                    f"than a single account can publish. Lengthen the schedule.")
    return ""


async def perform_publish(state: dict, caption: str, asset_path: str = "",
                          media_kind: str = "auto", asset_paths: list[str] | None = None,
                          placement: str = "feed") -> dict:
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

    # One asset or many (a carousel). asset_path stays the primary, for previews.
    paths = [p for p in (asset_paths or []) if p] or ([asset_path] if asset_path else [])
    asset_path = paths[0] if paths else ""
    placement = (placement or "feed").lower()
    kind = media_kind if media_kind != "auto" else kind_from_path(asset_path)
    logger.info("Publish requested: mode=%s platform=%s kind=%s placement=%s items=%d "
                "caption=%d chars asset=%s", mode.value, account.platform.value, kind,
                placement, len(paths), len(caption or ""), asset_path or "(none)")

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
    if len(paths) > 1 and not caps.supports_carousel:
        return {"error": "unsupported_placement",
                "message": f"{account.platform.value} cannot post several items in one post; "
                           f"publish one asset, or one post per item."}
    if len(paths) > caps.max_carousel_items:
        return {"error": "too_many_items",
                "message": f"{account.platform.value} allows at most "
                           f"{caps.max_carousel_items} items per post; you passed {len(paths)}."}
    if placement == "story" and not caps.supports_stories:
        return {"error": "unsupported_placement",
                "message": f"{account.platform.value} has no stories placement."}
    if placement not in {"feed", "story", "reel"}:
        return {"error": "unknown_placement",
                "message": f"placement must be feed, story or reel — got {placement!r}."}

    # Declaring media_kind="image" proves nothing — the file has to be there. This
    # used to fall through to the platform, which failed with a generic "needs a
    # media asset" that gave the agent nothing to act on.
    wanted = "video" if caps.supports_video and not caps.supports_image else (
        "image or video" if caps.supports_image else "media")
    if kind != "text" and not paths:
        return {"error": "no_media_attached",
                "message": (f"You asked to publish {kind} but attached no file. Call "
                            f"generate_{'video' if kind == 'video' else 'image'} (or save_media) "
                            f"in THIS run, then pass the asset_path it returns — media from a "
                            f"previous run is not available here.")}
    if not paths and not caps.supports_text:
        return {"error": "no_media_attached",
                "message": (f"{account.platform.value} cannot post without {wanted}. Produce it "
                            f"in this run and pass its asset_path.")}
    missing = [p for p in paths if not asset_exists(p)]
    if missing:
        return {"error": "asset_missing",
                "message": (f"{len(missing)} of {len(paths)} asset(s) do not exist: "
                            f"{', '.join(m.rsplit('/', 1)[-1] for m in missing[:3])}. Media is not "
                            f"carried over between runs — generate it again in this run rather "
                            f"than reusing a path you remember.")}

    # Refuse to post the agent's own error message. Checked on the RAW caption,
    # before the AI disclosure is appended.
    offending = meta_caption_reason(caption) if settings.publish_content_guard else ""
    if offending:
        logger.error("Refusing to publish a caption about the run's own failure "
                     "(matched %r). Caption: %s", offending, (caption or "")[:200])
        return {
            "error": "caption_describes_a_failure",
            "message": (f"This caption talks about the run itself ({offending!r}). That is not "
                        "a post — call report_failure to end the run instead, with the "
                        "diagnosis in its details. If you DO have real content that satisfies "
                        "the brief, rewrite the caption to be about that content only."),
        }

    # Convert the image locally if this platform won't accept it as-is. Done
    # here (before staging) so the preview, the approval queue and the live post
    # all reference the SAME converted file.
    # Every image in the post needs converting, not just the first.
    paths = [_normalize_image_for(p, caps, account.platform.value)
             if kind_from_path(p) == "image" else p for p in paths]
    asset_path = paths[0] if paths else ""

    # AI-content disclosure, applied HERE so it reaches every path — the dry-run
    # preview and the approval queue show exactly what would be posted, and the
    # model cannot skip it. See aismm/disclosure.py for the legal/platform basis.
    caption = disclosure.apply_to_caption(caption, caption_limit=caps.caption_limit,
                                          instruction=instruction)

    run.caption = caption
    run.asset_path = asset_path
    staged = StagedPost(
        instruction_id=instruction.id, account_id=account.id, run_id=run.id,
        caption=caption, asset_path=asset_path, media_kind=kind, placement=placement,
    )
    staged.set_asset_paths(paths)

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
    # Refuse a re-post of content this account already published. The agent is
    # supposed to know from its memory, but the memory is model-written prose and
    # a run that published then failed to record it made the next run post the
    # same panel twice. This is the deterministic backstop.
    digest = publish_ledger.fingerprint(paths, placement)
    already = publish_ledger.find(account, digest)
    if already:
        message = (f"This exact media was ALREADY published to "
                   f"{account.handle or account.platform.value} on "
                   f"{publish_ledger.describe_entry(already)}. Not posting it again. The item "
                   f"is DONE: call update_memory to record it as published and advance your "
                   f"position, then finish with report_failure if there is nothing new left "
                   f"to post this run.")
        logger.warning("Refusing a duplicate publish for %s (fingerprint %s, first posted %s)",
                       account.handle or account.external_id, digest[:12],
                       already.get("at", "?"))
        run.status = RunStatus.failed
        run.error = message
        run.log = (run.log + "\nREFUSED: duplicate of an already-published post.").strip()
        store.update_run(run)
        state["result"] = {"mode": "live", "error": message, "duplicate": True,
                           "url": already.get("url", "")}
        return {"error": "already_published", "message": message,
                "published_at": already.get("at", ""), "url": already.get("url", "")}

    try:
        access_token, _refresh = store.get_tokens(account.id)
        if not access_token:
            raise RuntimeError("account has no stored access token — reconnect it in the dashboard.")
        result = await platform.publish(
            access_token=access_token, account=account,
            caption=caption, asset_path=asset_path, media_kind=kind,
            instruction=instruction, asset_paths=paths, placement=placement,
        )
    except RateLimited as exc:
        # A volume refusal, not a content problem. Stop this account from trying
        # again on the next scheduled run — repeated attempts extend the block.
        # start() escalates on each strike, since a flat cooldown that keeps
        # expiring right before the next scheduled fire is not actually helping.
        held = cooldown.start(account, store, exc.retry_after_seconds,
                              reason=f"{account.platform.value} rate limit")
        advice = _schedule_advice(instruction)
        strikes = cooldown.strike_count(account)
        streak = (f" This is strike {strikes} — the cooldown doubles each time this "
                  f"recurs, capped at {cooldown.MAX_COOLDOWN_SECONDS // 3600}h."
                  if strikes > 1 else "")
        message = (f"{exc} — {account.platform.value} is refusing posts for volume reasons, "
                   f"not because of this content. Publishing is paused for "
                   f"{held // 60} minutes.{streak}{advice}")
        logger.error("Publish rate-limited: %s", message)
        run.status = RunStatus.failed
        run.error = message
        store.update_run(run)
        state["result"] = {"mode": "live", "error": message, "rate_limited": True}
        return {"error": "rate_limited", "message": message,
                "retry_after_minutes": held // 60}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Live publish failed")
        run.status = RunStatus.failed
        run.error = str(exc)
        store.update_run(run)
        state["result"] = {"mode": "live", "error": str(exc)}
        return {"error": "publish_failed", "message": str(exc)}

    # Record the outcome in CODE, before returning to the agent. The agent is told
    # to write its memory after publish returns; when it doesn't, this is still
    # what stops the next run from posting the same thing again.
    publish_ledger.record(account, store, digest, url=result.url,
                          external_id=result.external_id, instruction_id=instruction.id)

    # The post landed, but Instagram had answered media_publish with a rate-limit
    # error — the app IS being throttled even though this one got through. Back off
    # anyway, or the next scheduled run knocks again and Meta extends the block.
    reconciled = bool((result.raw or {}).get("reconciled"))
    publish_error = (result.raw or {}).get("publish_error", "")
    if reconciled:
        logger.warning("Publish reconciled for %s: Instagram errored (%s) but the post is live "
                       "at %s", account.handle or account.external_id, publish_error, result.url)
        cooldown.start(account, store, RECONCILED_COOLDOWN_SECONDS,
                       reason="published, but Instagram reported a rate limit")
    else:
        # A genuinely clean publish — no rate-limit signal at all — is what tells
        # us the block has actually lifted, not just that the cooldown expired.
        # Reset the strike streak so a later isolated refusal starts back at the
        # base cooldown instead of picking up where an old streak left off.
        cooldown.clear(account, store)

    staged.status = StagedStatus.published
    staged.external_url = result.url
    store.add_staged(staged)
    run.status = RunStatus.published
    run.external_url = result.url
    run.log = (run.log + f"\nPublished {kind} post: {result.url}"
               + (f"\nRECONCILED: Instagram reported an error ({publish_error}) but the post "
                  f"is live. Publishing paused so the next run does not knock again."
                  if reconciled else "")).strip()
    store.update_run(run)
    state["result"] = {"mode": "live", "url": result.url, "kind": kind,
                       **({"reconciled": True} if reconciled else {})}
    return {"status": "published", "mode": "live", "url": result.url,
            **({"note": f"Instagram reported {publish_error!r} but the post IS live."}
               if reconciled else {}),
            "reminder": ("This IS published and recorded. Call update_memory now to advance "
                         "your position past this item so the next run does not repeat it.")}


def _make_publish(state: dict):
    @function_tool
    async def publish(caption: str, asset_path: str = "", media_kind: str = "auto",
                      asset_paths: list[str] | None = None, placement: str = "feed") -> dict:
        """Publish (or stage) the finished post to the account this run targets.

        Call this exactly once, at the end, with the final caption and (optionally)
        an ``asset_path`` returned by ``generate_video`` / ``generate_image``.

        Args:
            caption: The post text / caption / title+description. Instagram
                stories carry no caption — put the words in the image instead.
            asset_path: Path to a generated media asset, or "" for a text-only post.
            media_kind: "auto" (infer from asset), or "text"/"image"/"video".

        Returns a status dict. The publish mode (dry-run / approval / live) is set
        on the instruction and enforced here — you do not control it.
        """
        return await perform_publish(state, caption, asset_path, media_kind,
                                     asset_paths=asset_paths, placement=placement)

    return publish


register_tool("publish", _make_publish)
