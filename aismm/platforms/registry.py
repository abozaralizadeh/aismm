"""Platform registry — maps a :class:`PlatformName` to its integration class.

Register a new network with :func:`register`; construct one (with its configured
OAuth app credentials) via :func:`get_platform`.
"""
from __future__ import annotations

from ..config import settings
from ..models import PlatformName
from .base import SocialPlatform

_PLATFORMS: dict[PlatformName, type[SocialPlatform]] = {}


def register(name: PlatformName, cls: type[SocialPlatform]) -> None:
    _PLATFORMS[name] = cls


def registered_platforms() -> list[PlatformName]:
    return list(_PLATFORMS)


def get_platform_class(name: PlatformName) -> type[SocialPlatform]:
    if name not in _PLATFORMS:
        raise KeyError(f"No platform registered for {name!r}")
    return _PLATFORMS[name]


def get_platform(name: PlatformName) -> SocialPlatform:
    """Instantiate a platform with its OAuth app credentials from settings."""
    cls = get_platform_class(name)
    creds = settings.platform_creds.get(
        name.value if isinstance(name, PlatformName) else str(name)
    )
    from ..config import PlatformCreds

    return cls(creds or PlatformCreds())
