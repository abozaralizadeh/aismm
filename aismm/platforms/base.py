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
    # --- placements beyond a single feed post -------------------------------- #
    supports_carousel: bool = False          # multi-item post
    supports_stories: bool = False
    # Can a long caption be published as a chain of linked posts? On X this is
    # the difference between a 280-character truncation and the whole thought.
    # `caption_limit` still bounds ONE post; the usable total is
    # caption_limit * max_thread_posts.
    supports_threads: bool = False
    max_thread_posts: int = 1
    max_carousel_items: int = 10
    supports_comments: bool = False          # read/reply/moderate
    supports_insights: bool = False
    # Can the account LIKE a comment/post it is engaging with? Only some
    # platforms expose this over their API — X does (POST /2/users/:id/likes);
    # Instagram's Graph API, YouTube's Data API and TikTok's app API offer no
    # like-a-comment endpoint at all, so their tools do not pretend to.
    supports_liking: bool = False

    image_formats: tuple[str, ...] = ()      # () = anything
    max_image_bytes: int | None = None
    min_image_ratio: float | None = None     # width / height
    max_image_ratio: float | None = None
    max_image_width: int | None = None
    # Stories are a different shape from the feed — 9:16 (0.5625) is the native
    # one. Padding a story to the FEED's minimum ratio publishes it with bars
    # down both sides, so a platform whose story limits differ declares them here
    # and `perform_publish` picks the set that matches the placement.
    story_min_image_ratio: float | None = None
    story_max_image_ratio: float | None = None


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

    async def fetch_identities(self, access_token: str) -> list[Identity]:
        """EVERY profile this authorization covers, not just the first.

        Most platforms authorize exactly one profile, so the default wraps
        :meth:`fetch_identity`. Instagram is the exception: one Facebook login can
        administer several Pages, each with its own Instagram account and its own
        page token — and because a Meta app holds only ONE grant per Facebook
        user, connecting them one at a time means each authorization replaces the
        last and breaks the accounts already connected. Taking them all from a
        single authorization is what avoids that entirely.
        """
        return [await self.fetch_identity(access_token)]

    def after_publish(self, *, account: Account, store, result: "PublishResult") -> None:
        """Per-platform bookkeeping once a live post has landed. Default: nothing.

        An extension point rather than another branch in ``perform_publish``:
        only X needs it today (advancing the community rotation), and the publish
        tool should not grow a special case per platform.
        """

    async def inspect_token(self, access_token: str, account: Account | None = None) -> dict:
        """What this stored token actually is — the "Check permissions" button.

        Every platform gets an answer. The default proves the token by making the
        cheapest authenticated call there is (``fetch_identity``) and reports the
        scopes recorded when the account was connected; a platform with real
        token introspection overrides this with something better (Instagram asks
        Graph ``/debug_token``, which is the only way to tell a PAGE token from a
        USER one).

        Returns ``{}`` only when it genuinely cannot tell. Never raises — it is a
        diagnostic, and one that cannot answer must not also break the page.
        """
        granted = list((account.meta or {}).get("granted_scopes") or []) if account else []
        try:
            identity = await self.fetch_identity(access_token)
        except Exception as exc:  # noqa: BLE001 - the failure IS the diagnosis
            return {"is_valid": False, "type": "USER", "scopes": granted,
                    "error": str(exc)[:300], "source": "identity check"}
        return {"is_valid": True, "type": "USER", "scopes": granted,
                "handle": identity.handle, "profile_id": identity.external_id,
                "source": "identity check"}

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
        asset_paths: list[str] | None = None,
        placement: str = "feed",
    ) -> PublishResult:
        """Publish a post. ``media_kind`` is one of text|image|video.

        ``instruction`` is passed so a platform can honour per-instruction
        settings — today the AI-disclosure toggle that drives the native
        platform flags (see :mod:`aismm.disclosure`).

        ``asset_paths`` carries more than one file (a carousel); ``placement`` is
        ``feed`` / ``story`` / ``reel``. Platforms that support neither can ignore
        both — the publish tool checks ``Capabilities`` before calling.
        """

    async def reply_to_target(self, access_token: str, account: Account, *,
                              target_type: str, target_id: str, text: str) -> dict:
        """Reply to a comment / mention / reply — the engagement counterpart of
        :meth:`publish`, and the one method the mode-gated engagement flow
        (:mod:`aismm.engagement`) calls once a live reply is due.

        ``target_type`` is ``comment`` / ``mention`` / ``reply`` (later ``dm``);
        ``target_id`` is the platform id of the thing being answered. Returns a
        dict that SHOULD carry a ``url`` (the reply's permalink where one exists)
        and/or an ``id`` — :mod:`aismm.engagement` records the url in the ledger.

        The default refuses: a platform without a comment API declares
        ``supports_comments=False`` and never reaches here (the gate checks
        capabilities first), but a defensive message beats an ``AttributeError``
        if it does — the same lesson as ``publish`` growing ``asset_paths``.
        """
        raise RuntimeError(
            f"{self.name.value} does not support replying to a {target_type} here.")

    async def like_target(self, access_token: str, account: Account, *,
                          target_type: str, target_id: str, like: bool = True) -> dict:
        """Like (``like=True``) or un-like (``like=False``) a comment/post.

        The lightweight counterpart of :meth:`reply_to_target` — an
        acknowledgement that needs no words, which the engagement flow uses when a
        comment is warm but does not call for a reply. It is an immediate write
        (like moderation), NOT gated by ``publish_mode``: a like is not outbound
        content the way a post or a reply is.

        Only a platform declaring ``supports_liking=True`` implements this; the
        default refuses, and the tool layer never offers a like tool on a platform
        without it. Returns a dict that SHOULD carry ``liked`` (the resulting
        state) and the ``id`` that was liked.
        """
        raise RuntimeError(
            f"{self.name.value} has no API to like a {target_type}.")

    async def post_exists(self, access_token: str, account: Account,
                          external_id: str) -> bool | None:
        """Is this post still PUBLICLY on the account?

        ``True`` yes, ``False`` no — deleted **or archived**, ``None`` cannot tell
        (this platform has no way to check, or the check itself failed).

        "Archived" counts as gone on purpose: a post the human pulled off the grid
        is not something followers can see, so its content should be publishable
        again — the same reasoning as a deletion.

        The [publish ledger](../publish_ledger.py) is only a local *record* of what
        we posted; the account itself is the authority. The default is ``None`` so
        a platform without an implementation keeps the ledger-only behaviour.
        """
        return None
