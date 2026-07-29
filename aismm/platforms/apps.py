"""Resolving which OAuth app credentials to use for a connect.

Credentials can come from two places:

* a :class:`~aismm.models.PlatformApp` row, managed in the dashboard — several
  per platform, so one deployment can serve several brands or clients;
* ``.env`` (:class:`~aismm.config.PlatformCreds`) — the original single-app
  setup, still honoured so existing deployments keep working untouched.

The dashboard offers every configured app when connecting an account, and the
account records which one authorised it (in its ``meta``), so it is always clear
where a connection came from.
"""
from __future__ import annotations

from ..config import PlatformCreds, settings
from ..models import PlatformApp, PlatformName


def env_creds(platform: PlatformName) -> PlatformCreds:
    """Credentials from ``.env`` for this platform (may be unconfigured)."""
    return settings.platform_creds.get(platform.value) or PlatformCreds()


def app_creds(app: PlatformApp, store) -> PlatformCreds:
    """Credentials from a dashboard-managed app row (secret decrypted here)."""
    return PlatformCreds(
        client_id=app.client_id,
        client_secret=store.get_app_secret(app.id),
        extra=app.extra,
    )


def available_apps(platform: PlatformName, store) -> list[PlatformApp]:
    """Enabled dashboard apps for a platform, oldest first."""
    return [a for a in store.list_platform_apps(platform) if a.enabled]


def resolve_creds(platform: PlatformName, store, app_id: str | None = None) -> PlatformCreds:
    """Pick the credentials for a connect.

    An explicit ``app_id`` wins; otherwise the first enabled app for the
    platform; otherwise ``.env``. Returns empty creds when nothing is set up, so
    callers can report "not configured" rather than crash.
    """
    if app_id:
        app = store.get_platform_app(app_id)
        if app and app.platform == platform:
            return app_creds(app, store)
    apps = available_apps(platform, store)
    if apps:
        return app_creds(apps[0], store)
    return env_creds(platform)


def connection_options(platform: PlatformName, store) -> list[dict]:
    """Everything the dashboard can offer as a "connect with…" choice.

    Each entry is ``{app_id, label, configured}``. ``app_id`` is empty for the
    ``.env`` credentials, which appear only when no dashboard app exists (having
    both would be a confusing duplicate).
    """
    options = [
        {"app_id": app.id, "label": app.label, "configured": bool(app.client_id)}
        for app in available_apps(platform, store)
    ]
    if not options:
        creds = env_creds(platform)
        options.append({"app_id": "", "label": f"{platform.value} app (from .env)",
                        "configured": creds.configured})
    return options


def is_configured(platform: PlatformName, store) -> bool:
    return any(option["configured"] for option in connection_options(platform, store))
