"""Social platform integrations.

Importing this package registers the built-in platforms (Instagram, X/Twitter,
YouTube, TikTok). Add a network by subclassing :class:`SocialPlatform` and calling
:func:`aismm.platforms.registry.register`.
"""
from __future__ import annotations

from . import instagram, tiktok, twitter, youtube  # noqa: F401  (registration side effects)
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import get_platform, register, registered_platforms

__all__ = [
    "SocialPlatform",
    "Capabilities",
    "PublishResult",
    "Identity",
    "get_platform",
    "register",
    "registered_platforms",
]
