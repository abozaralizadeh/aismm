"""Social platform integrations.

Importing this package registers the built-in platforms (Instagram, X/Twitter,
YouTube, TikTok, LinkedIn, Facebook). Add a network by subclassing
:class:`SocialPlatform` and calling :func:`aismm.platforms.registry.register`.
"""
from __future__ import annotations

from . import (  # noqa: F401  (registration side effects)
    facebook,
    instagram,
    linkedin,
    tiktok,
    twitter,
    youtube,
)
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
