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

from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    # Whether a server process (see aismm/wsgi.py) also runs the scheduler.
    enable_scheduler: bool = True

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
        return f"{self.dashboard.base_url.rstrip('/')}/oauth/{platform}/callback"


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
        ),
        platform_creds=_load_platform_creds(),
        enable_scheduler=_bool(os.getenv("AISMM_ENABLE_SCHEDULER"), True),
    )


# Single process-wide settings instance.
settings = load_settings()


def ensure_dirs() -> None:
    """Create the data + assets directories if they don't exist yet."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.assets_dir.mkdir(parents=True, exist_ok=True)
