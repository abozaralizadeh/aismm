"""TikTok engagement tools — deliberately gated OFF, and honest about why.

Unlike Instagram, X and YouTube, TikTok does **not** expose a comment
list/reply API to general third-party apps: the comment endpoints are part of a
restricted programme that requires TikTok's audit and a comment scope most apps
never get. Rather than ship reply tools that would 4xx on every call — or, worse,
quietly do nothing — this module keeps the same self-opt-out shape the Sora tool
uses when unconfigured: the factories return ``None`` (so no TikTok comment tools
are ever built) and log ONCE, clearly, that TikTok engagement is unavailable
without the gated scope. That way an engage run on a TikTok account fails with a
visible reason instead of pretending to work.

``TikTok.capabilities.supports_comments`` is ``False`` for the same reason, so the
engagement gate (:mod:`aismm.engagement`) refuses a TikTok reply defensively even
if one ever reached it. If TikTok grants your app the comment scope, wire the
methods onto :class:`aismm.platforms.tiktok.TikTok` (``list_comments`` /
``reply_to_target``), flip ``supports_comments`` to ``True``, and gate these
factories on the scope being present in ``account.meta['granted_scopes']`` — the
same pattern the Instagram scope split uses.
"""
from __future__ import annotations

import logging

from ..models import PlatformName
from ..platforms.registry import get_platform
from .registry import register_tool

logger = logging.getLogger("aismm.tools.tiktok")

_warned = False


def _unavailable(state: dict):
    """Explain (once) why a TikTok run gets no engagement tools, then opt out."""
    global _warned
    account = state.get("account")
    if account is None or account.platform is not PlatformName.tiktok:
        return None  # not a TikTok run — silently irrelevant, like every other guard
    if not get_platform(PlatformName.tiktok).capabilities.supports_comments and not _warned:
        _warned = True
        logger.warning(
            "TikTok engagement (reading/replying to comments) is unavailable: TikTok does "
            "not grant a comment API to this app. An engagement run on a TikTok account "
            "cannot answer comments — use Instagram, X or YouTube for that.")
    return None


register_tool("tiktok_comments", _unavailable)
register_tool("tiktok_reply_to_comment", _unavailable)
