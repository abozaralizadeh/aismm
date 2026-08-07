"""Central configuration for AISMM, loaded from environment / .env.

Everything the app needs to know about *where* things live and *how* to reach
external services is resolved here once, so the rest of the codebase never calls
``os.getenv`` directly. Mirrors the SandBox convention of ``python-dotenv`` +
``os.environ`` (no heavy settings framework), but collected behind a typed
``Settings`` object for clarity.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path_prefix(value: str | None) -> str:
    """Normalize a reverse-proxy path prefix to ``/path`` or ``""``."""
    raw = (value or "").strip()
    if not raw or raw == "/":
        return ""
    if "://" in raw or "?" in raw or "#" in raw:
        raise ValueError("REVERSE_PROXY_PREFIX must be a URL path, for example /aismm")
    parts = [part for part in raw.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("REVERSE_PROXY_PREFIX cannot contain '.' or '..' path segments")
    return "/" + "/".join(parts)


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "azure"  # "azure" | "apim"
    model: str = "gpt-4o"
    # Azure direct
    azure_api_key: str = ""
    azure_endpoint: str = ""
    azure_api_version: str = "2025-04-01-preview"
    # APIM
    apim_base_url: str = ""
    apim_subscription_key: str = ""
    apim_key_header: str = "api-key"
    apim_api_version: str = "2025-04-01-preview"


@dataclass(frozen=True)
class ImageSettings:
    api_key: str = ""
    endpoint: str = ""
    api_version: str = "2025-04-01-preview"
    model: str = "gpt-image-1"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.endpoint)


@dataclass(frozen=True)
class SoraSettings:
    endpoints: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=lambda: ["sora-2"])
    api_version: str = "preview"
    # How many resources one clip may try before giving up. 0 = auto
    # (min(pool size, 3)) — enough to route around a dead endpoint without
    # burning a whole run on a pool of slow timeouts.
    max_attempts: int = 0

    def pool(self) -> list[dict]:
        """Return the Sora resource pool as a list of {endpoint, key, model}.

        Keys/models align to endpoints by index; a single key/model applies to
        all endpoints (see GenBox's job-affinity round-robin rationale).
        """
        pool: list[dict] = []
        for i, endpoint in enumerate(self.endpoints):
            key = self.keys[i] if i < len(self.keys) else (self.keys[0] if self.keys else "")
            model = self.models[i] if i < len(self.models) else self.models[0]
            pool.append({"endpoint": endpoint, "key": key, "model": model})
        return pool

    @property
    def enabled(self) -> bool:
        return any(r["endpoint"] and r["key"] for r in self.pool())


@dataclass(frozen=True)
class DashboardSettings:
    host: str = "127.0.0.1"
    port: int = 8787
    base_url: str = "http://127.0.0.1:8787"
    secret_key: str = "change-me"
    reverse_proxy_prefix: str = ""

    @property
    def public_base_url(self) -> str:
        """Dashboard URL, including the configured reverse-proxy path prefix."""
        base_url = self.base_url.rstrip("/")
        prefix = self.reverse_proxy_prefix
        if not prefix:
            return base_url

        parsed = urlsplit(base_url)
        base_path = parsed.path.rstrip("/")
        # Keep supporting deployments that already included the prefix in
        # DASHBOARD_BASE_URL before REVERSE_PROXY_PREFIX was introduced.
        if base_path == prefix or base_path.endswith(prefix):
            return base_url
        return urlunsplit(parsed._replace(path=f"{base_path}{prefix}"))

    def external_url(self, path: str = "") -> str:
        """Build an absolute public dashboard URL under the proxy prefix."""
        if not path:
            return self.public_base_url
        return f"{self.public_base_url}/{path.lstrip('/')}"


@dataclass(frozen=True)
class DisclosureSettings:
    """Whether and how posts are labelled as AI-generated.

    On by default: the EU AI Act's Article 50 transparency duties apply from
    2 August 2026, and every major platform has its own AI-labelling rule. See
    :mod:`aismm.disclosure`.
    """

    enabled: bool = True
    text: str = "🤖 AI-generated"
    separator: str = "\n\n"
    # Append the label to the caption as well as setting the platform's own flag.
    # OFF by default: all four platforms render a native label from the
    # publishing API, and that label is stronger than a sentence of prose — it is
    # what the platform shows and what its policies key on. Set
    # AI_DISCLOSURE_CAPTION=1 to add the line too.
    in_caption: bool = False


@dataclass(frozen=True)
class AzureStorageSettings:
    """Azure Table + Blob storage, wired the way the SandBox projects do it.

    One storage account serves both: a single **Table** holds every entity
    (PartitionKey = entity type, RowKey = id — SandBox's convention) and a
    **Blob container** holds generated media. The blob container doubles as the
    PUBLIC media URL Instagram needs, which the dashboard otherwise has to serve.
    """

    connection_string: str = ""
    table_name: str = "aismm"
    container_name: str = "aismm-media"

    @property
    def configured(self) -> bool:
        return bool(self.connection_string)


@dataclass(frozen=True)
class AuthSettings:
    """Single-sign-on for the dashboard itself (generic OpenID Connect).

    Any OIDC provider works — Google, Microsoft Entra ID, Okta, Auth0, Keycloak —
    because the endpoints are read from the issuer's discovery document. Access is
    granted only to identities on the allowlist; a login that authenticates but
    matches nothing is refused.
    """

    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = field(default_factory=lambda: ["openid", "email", "profile"])
    allowed_emails: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    provider_name: str = "SSO"          # button label: "Sign in with <name>"
    session_hours: int = 12
    enabled_override: bool | None = None  # AUTH_ENABLED forces on/off; None = auto

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id and self.client_secret)

    @property
    def enabled(self) -> bool:
        """On by default as soon as an OIDC app is configured."""
        if self.enabled_override is not None:
            return self.enabled_override
        return self.configured

    @property
    def has_allowlist(self) -> bool:
        return bool(self.allowed_emails or self.allowed_domains)

    def allows(self, email: str) -> bool:
        """Is this identity allowed in? Fails closed when no allowlist is set."""
        addr = (email or "").strip().lower()
        if not addr or not self.has_allowlist:
            return False
        if addr in {e.lower() for e in self.allowed_emails}:
            return True
        domain = addr.rpartition("@")[2]
        return bool(domain) and domain in {d.lower().lstrip("@") for d in self.allowed_domains}


@dataclass(frozen=True)
class PlatformCreds:
    """OAuth app credentials for one platform (client id/secret + extras)."""

    client_id: str = ""
    client_secret: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True)
class Settings:
    env: str
    data_dir: Path
    token_key: str
    llm: LLMSettings
    image: ImageSettings
    sora: SoraSettings
    dashboard: DashboardSettings
    platform_creds: dict[str, PlatformCreds]
    auth: AuthSettings = field(default_factory=AuthSettings)
    azure_storage: AzureStorageSettings = field(default_factory=AzureStorageSettings)
    disclosure: DisclosureSettings = field(default_factory=DisclosureSettings)
    # "local" (SQLite + disk) or "azure" (Table + Blob). "auto" picks azure as
    # soon as a storage connection string is present.
    store_backend: str = "auto"
    # Whether a server process (see aismm/wsgi.py) also runs the scheduler.
    enable_scheduler: bool = True
    # Carry-over memory is summarized once it grows past this many characters.
    memory_max_chars: int = 6000
    # A ceiling on ONE run, not a target. It exists because APScheduler runs jobs
    # with max_instances=1: a run that never returns silences its instruction
    # permanently and leaks a pool thread. Generous by default — a nine-shot video
    # sequence is legitimately tens of minutes of Sora rendering — but never
    # absent. Set RUN_TIMEOUT_SECONDS=0 to disable it entirely, knowing that a
    # single hung run then blocks that instruction until the service restarts.
    # Days a generated asset stays on local disk once blob storage has a copy.
    # The local folder is a CACHE when blob is configured; without pruning a VM
    # fills up and the next run fails trying to write its media. 0 disables it.
    asset_retention_days: int = 14
    run_timeout_seconds: int = 7200
    # How long ONE Sora job may take before it is abandoned and retried elsewhere.
    sora_job_timeout_seconds: int = 1800
    # Refuse to publish a caption that narrates the run's own failure. See
    # tools/publish_tool.meta_caption_reason.
    publish_content_guard: bool = True
    # What the duplicate guard does when it CANNOT confirm whether the earlier
    # post is still on the account (rate limited, network trouble, no token).
    # False (default) publishes anyway: for sequential content — a comic posted
    # panel by panel — a wrongly skipped item breaks the running order, while a
    # duplicate is two taps to delete. Set true where an accidental duplicate
    # costs more than a gap. A CONFIRMED duplicate is always refused either way.
    publish_duplicate_guard_strict: bool = False
    # Override the Instagram OAuth scopes (comma/space separated). Meta rejects
    # the ENTIRE authorization dialog if any one scope is unavailable to the app
    # ("Invalid Scopes: …"), so the default asks only for what publishing needs
    # plus comments. Add instagram_manage_insights here once App Review has
    # granted it. See aismm/platforms/instagram.py.
    instagram_scopes: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aismm.sqlite"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    def redirect_uri(self, platform: str) -> str:
        return self.dashboard.external_url(f"oauth/{platform}/callback")

    @property
    def use_azure_store(self) -> bool:
        """Should state live in Azure Table storage rather than local SQLite?"""
        backend = (self.store_backend or "auto").strip().lower()
        if backend == "azure":
            return True
        if backend == "local":
            return False
        return self.azure_storage.configured      # auto

    @property
    def auth_redirect_uri(self) -> str:
        """Redirect URI to register with the SSO provider."""
        return self.dashboard.external_url("auth/callback")


def _load_platform_creds() -> dict[str, PlatformCreds]:
    return {
        "instagram": PlatformCreds(
            client_id=os.getenv("INSTAGRAM_APP_ID", ""),
            client_secret=os.getenv("INSTAGRAM_APP_SECRET", ""),
        ),
        "twitter": PlatformCreds(
            client_id=os.getenv("TWITTER_CLIENT_ID", ""),
            client_secret=os.getenv("TWITTER_CLIENT_SECRET", ""),
            extra={
                "api_key": os.getenv("TWITTER_API_KEY", ""),
                "api_secret": os.getenv("TWITTER_API_SECRET", ""),
            },
        ),
        "youtube": PlatformCreds(
            client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        ),
        "tiktok": PlatformCreds(
            client_id=os.getenv("TIKTOK_CLIENT_KEY", ""),
            client_secret=os.getenv("TIKTOK_CLIENT_SECRET", ""),
        ),
        "linkedin": PlatformCreds(
            client_id=os.getenv("LINKEDIN_CLIENT_ID", ""),
            client_secret=os.getenv("LINKEDIN_CLIENT_SECRET", ""),
        ),
        # Facebook Pages ride the SAME Meta app as Instagram, so an existing Meta
        # setup connects Pages with no new credentials — its own env vars win
        # when a separate app is preferred.
        "facebook": PlatformCreds(
            client_id=os.getenv("FACEBOOK_APP_ID", "") or os.getenv("INSTAGRAM_APP_ID", ""),
            client_secret=(os.getenv("FACEBOOK_APP_SECRET", "")
                           or os.getenv("INSTAGRAM_APP_SECRET", "")),
        ),
    }


def load_settings() -> Settings:
    data_dir = Path(os.getenv("AISMM_DATA_DIR", "./data")).expanduser().resolve()
    return Settings(
        env=os.getenv("ENV", "dev"),
        data_dir=data_dir,
        token_key=os.getenv("AISMM_TOKEN_KEY", ""),
        llm=LLMSettings(
            provider=os.getenv("LLM_PROVIDER", "azure").strip().lower(),
            model=os.getenv("AZURE_OPENAI_MODEL", "gpt-4o"),
            azure_api_key=os.getenv("AZURE_OPENAI_API_KEY", ""),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
            azure_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
            apim_base_url=os.getenv("APIM_BASE_URL", ""),
            apim_subscription_key=os.getenv("APIM_SUBSCRIPTION_KEY", ""),
            apim_key_header=os.getenv("APIM_KEY_HEADER", "api-key"),
            apim_api_version=os.getenv("APIM_API_VERSION", "2025-04-01-preview"),
        ),
        image=ImageSettings(
            api_key=os.getenv("AZURE_OPENAI_API_KEY_DALLE", ""),
            endpoint=os.getenv("AZURE_OPENAI_ENDPOINT_DALLE", ""),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
            model=os.getenv("AZURE_OPENAI_MODEL_DALLE", "gpt-image-1"),
        ),
        sora=SoraSettings(
            endpoints=_split_csv(os.getenv("AZURE_OPENAI_ENDPOINT_SORA")),
            keys=_split_csv(os.getenv("AZURE_OPENAI_API_KEY_SORA")),
            models=_split_csv(os.getenv("AZURE_OPENAI_MODEL_SORA")) or ["sora-2"],
            api_version=os.getenv("AZURE_OPENAI_API_VERSION_SORA", "preview"),
            max_attempts=int(os.getenv("SORA_MAX_ATTEMPTS", "0") or 0),
        ),
        dashboard=DashboardSettings(
            host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
            port=int(os.getenv("DASHBOARD_PORT", "8787")),
            base_url=os.getenv("DASHBOARD_BASE_URL", "http://127.0.0.1:8787"),
            secret_key=os.getenv("FLASK_SECRET_KEY", "change-me"),
            reverse_proxy_prefix=_path_prefix(os.getenv("REVERSE_PROXY_PREFIX")),
        ),
        platform_creds=_load_platform_creds(),
        auth=AuthSettings(
            issuer=os.getenv("AUTH_OIDC_ISSUER", "").strip().rstrip("/"),
            client_id=os.getenv("AUTH_OIDC_CLIENT_ID", "").strip(),
            client_secret=os.getenv("AUTH_OIDC_CLIENT_SECRET", "").strip(),
            scopes=_split_csv(os.getenv("AUTH_OIDC_SCOPES")) or ["openid", "email", "profile"],
            allowed_emails=_split_csv(os.getenv("AUTH_ALLOWED_EMAILS")),
            allowed_domains=_split_csv(os.getenv("AUTH_ALLOWED_DOMAINS")),
            provider_name=os.getenv("AUTH_PROVIDER_NAME", "SSO").strip() or "SSO",
            session_hours=int(os.getenv("AUTH_SESSION_HOURS", "12") or 12),
            enabled_override=(None if not os.getenv("AUTH_ENABLED", "").strip()
                              else _bool(os.getenv("AUTH_ENABLED"), True)),
        ),
        azure_storage=AzureStorageSettings(
            # SandBox shares one storage account across its projects under the
            # lowercase `connection_string` name; accept that verbatim so an
            # existing SandBox .env drops straight in, with an explicit alias.
            connection_string=(os.getenv("AZURE_STORAGE_CONNECTION_STRING")
                               or os.getenv("connection_string") or "").strip(),
            table_name=(os.getenv("AISMM_TABLE_NAME")
                        or os.getenv("aismm_table_name") or "aismm").strip(),
            container_name=(os.getenv("AISMM_BLOB_NAME")
                            or os.getenv("aismm_blob_name") or "aismm-media").strip(),
        ),
        disclosure=DisclosureSettings(
            enabled=_bool(os.getenv("AI_DISCLOSURE_ENABLED"), True),
            text=os.getenv("AI_DISCLOSURE_TEXT", "🤖 AI-generated"),
            separator=os.getenv("AI_DISCLOSURE_SEPARATOR", "\n\n").replace("\\n", "\n"),
            in_caption=_bool(os.getenv("AI_DISCLOSURE_CAPTION"), False),
        ),
        store_backend=os.getenv("STORE_BACKEND", "auto").strip().lower() or "auto",
        enable_scheduler=_bool(os.getenv("AISMM_ENABLE_SCHEDULER"), True),
        memory_max_chars=int(os.getenv("MEMORY_MAX_CHARS", "6000") or 6000),
        asset_retention_days=int(os.getenv("ASSET_RETENTION_DAYS", "14") or 0),
        run_timeout_seconds=int(os.getenv("RUN_TIMEOUT_SECONDS", "7200") or 0),
        sora_job_timeout_seconds=int(
            os.getenv("SORA_JOB_TIMEOUT_SECONDS", "1800") or 1800),
        publish_content_guard=_bool(os.getenv("PUBLISH_CONTENT_GUARD"), True),
        publish_duplicate_guard_strict=_bool(
            os.getenv("PUBLISH_DUPLICATE_GUARD_STRICT"), False),
        instagram_scopes=os.getenv("INSTAGRAM_SCOPES", "").strip(),
    )


# Single process-wide settings instance.
settings = load_settings()


def ensure_dirs() -> None:
    """Create the data + assets directories if they don't exist yet."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.assets_dir.mkdir(parents=True, exist_ok=True)
