"""AI-content disclosure — labelling posts as AI-generated.

**Why this is enforced in code, not asked of the model.** From **2 August 2026**
the EU AI Act's Article 50 transparency obligations apply. A deployer publishing
AI-generated image/audio/video that resembles real people must disclose it, and
AI-generated *text* published to inform the public on matters of public interest
must be disclosed unless it went through substantive human review with someone
holding editorial responsibility. Every major platform has its own rule on top
(Meta's "AI info", TikTok's AIGC label, YouTube's altered-content disclosure,
X's "Made with AI"). A guardrail that the model can forget is not a guardrail —
so this sits next to the publish-mode gate, applied to every post on every path.

Two layers, because a caption line alone is not what the platforms key on:

1. **Native platform flags** — the real, platform-rendered labels, set through the
   publishing APIs where one exists:
     * TikTok  ``post_info.is_aigc = true``     -> "Creator labeled as AI-generated"
     * YouTube ``status.containsSyntheticMedia = true`` -> altered/synthetic disclosure
   Instagram and X have no per-post field in their publishing APIs today (Meta
   infers it from C2PA/IPTC metadata and its own detection), so for those the
   caption line is the disclosure.
2. **A caption suffix** — visible to a human at first exposure, which is what
   Article 50 asks for, and the only lever on platforms without an API flag.

The suffix is appended within the platform's caption limit: the *caption* is
trimmed to make room, never the label.

This is engineering, not legal advice — the default text and whether the rules
apply to your posts are your call. ``AI_DISCLOSURE_ENABLED=0`` turns it off.
"""
from __future__ import annotations

import logging
import re

from .config import settings

logger = logging.getLogger("aismm.disclosure")

# Phrases that already count as a disclosure, so we don't double-label a caption
# the agent (or the brief) already labelled.
_EXISTING = re.compile(
    r"(ai[\s\-]?generated|generated\s+by\s+ai|made\s+with\s+ai|ai[\s\-]?made|"
    r"created\s+with\s+ai|synthetic\s+media|#ai\b|ai\s+info)",
    re.IGNORECASE,
)


def enabled(instruction=None) -> bool:
    """Is disclosure on for this post?

    The global switch is the master (``AI_DISCLOSURE_ENABLED``); an instruction
    can opt *out* below it, never override it back on.
    """
    if not settings.disclosure.enabled:
        return False
    if instruction is not None and not getattr(instruction, "disclose_ai", True):
        return False
    return True


def already_disclosed(caption: str) -> bool:
    return bool(_EXISTING.search(caption or ""))


def label() -> str:
    return settings.disclosure.text.strip()


def apply_to_caption(caption: str, *, caption_limit: int | None = None,
                     instruction=None) -> str:
    """Append the AI disclosure to a caption, fitting it inside the limit.

    Returns the caption unchanged when disclosure is off (globally or for this
    instruction) or the text already carries a disclosure.
    """
    text = (caption or "").strip()
    if not enabled(instruction):
        return text
    suffix = label()
    if not suffix or already_disclosed(text):
        return text

    separator = settings.disclosure.separator
    addition = f"{separator}{suffix}"

    if caption_limit and len(text) + len(addition) > caption_limit:
        # Trim the CAPTION, never the label — a truncated disclosure is worse
        # than a shorter post.
        room = caption_limit - len(addition)
        if room <= 0:
            logger.warning("Caption limit %s leaves no room for the AI disclosure", caption_limit)
            return suffix[:caption_limit]
        text = text[:room].rstrip()
    return f"{text}{addition}"


def native_flags(platform_name: str, instruction=None) -> dict:
    """Publishing-API fields that carry the platform's own AI label.

    Empty for platforms whose API has no such field — there the caption suffix
    is the disclosure.
    """
    if not enabled(instruction):
        return {}
    return {
        "tiktok": {"is_aigc": True},
        "youtube": {"containsSyntheticMedia": True},
    }.get(platform_name, {})
