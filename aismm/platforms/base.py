"""Base class + value types shared by all platform integrations.

Subclasses declare their OAuth endpoints/scopes and capabilities as class
attributes and implement :meth:`fetch_identity` and :meth:`publish`. The generic
OAuth methods (authorize URL, code exchange, refresh) are provided here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..auth import oauth
from ..auth.oauth import TokenBundle
from ..config import PlatformCreds
from ..models import Account, PlatformName


@dataclass
class Capabilities:
    supports_text: bool
    supports_image: bool
    supports_video: bool
    needs_public_media_url: bool
    default_orientation: str          # "portrait" | "landscape"
    caption_limit: int
    notes: str = ""

    # --- image constraints, enforced locally before publishing --------------- #
    # Defaults are permissive: a platform that doesn't declare these accepts
    # whatever the agent produced. Instagram is the strict one (JPEG only,
    # 8 MB, 4:5–1.91:1, width <=1440) and a mismatch there surfaces as an
    # opaque "Media download has failed", so we normalize first.
    # See aismm/media.py.
    image_formats: tuple[str, ...] = ()      # () = anything
    max_image_bytes: int | None = None
    min_image_ratio: float | None = None     # width / height
    max_image_ratio: float | None = None
    max_image_width: int | None = None


@dataclass
class Identity:
    external_id: str
    handle: str
    meta: dict = field(default_factory=dict)


@dataclass
class PublishResult:
    url: str
    external_id: str = ""
    raw: dict = field(default_factory=dict)


class SocialPlatform(ABC):
    name: PlatformName
    capabilities: Capabilities

    # OAuth configuration (overridden per platform)
    auth_endpoint: str = ""
    token_endpoint: str = ""
    scopes: list[str] = []
    use_pkce: bool = False
    token_auth_style: str = "body"     # "body" | "basic"
    scope_sep: str = " "
    extra_authorize_params: dict = {}

    def __init__(self, creds: PlatformCreds) -> None:
        self.creds = creds

    # --- OAuth (generic defaults) ----------------------------------------- #
    def authorize_url(self, *, redirect_uri: str, state: str, code_challenge: str | None = None) -> str:
        return oauth.build_authorize_url(
            self.auth_endpoint,
            client_id=self.creds.client_id,
            redirect_uri=redirect_uri,
            scopes=self.scopes,
            state=state,
            code_challenge=code_challenge if self.use_pkce else None,
            scope_sep=self.scope_sep,
            extra_params=dict(self.extra_authorize_params) or None,
        )

    async def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str | None = None) -> TokenBundle:
        return await oauth.exchange_code(
            self.token_endpoint,
            client_id=self.creds.client_id,
            client_secret=self.creds.client_secret,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier if self.use_pkce else None,
            auth_style=self.token_auth_style,
        )

    async def refresh(self, refresh_token: str) -> TokenBundle:
        return await oauth.refresh_token(
            self.token_endpoint,
            client_id=self.creds.client_id,
            client_secret=self.creds.client_secret,
            refresh_token=refresh_token,
            auth_style=self.token_auth_style,
        )

    # --- Platform-specific ------------------------------------------------ #
    @abstractmethod
    async def fetch_identity(self, access_token: str) -> Identity:
        """Look up the connected profile (id + display handle + platform meta)."""

    @abstractmethod
    async def publish(
        self,
        *,
        access_token: str,
        account: Account,
        caption: str,
        asset_path: str,
        media_kind: str,
        instruction=None,
    ) -> PublishResult:
        """Publish a post. ``media_kind`` is one of text|image|video.

        ``instruction`` is passed so a platform can honour per-instruction
        settings — today the AI-disclosure toggle that drives the native
        platform flags (see :mod:`aismm.disclosure`).
        """
