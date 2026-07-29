"""Resolving which OAuth app credentials to use for a connect.

Credentials come from two places, and **both stay available at the same time**:

* ``.env`` (:class:`~aismm.config.PlatformCreds`) — the original single-app
  setup, and the default;
* :class:`~aismm.models.PlatformApp` rows, managed in the dashboard — several
  per platform, so one deployment can serve several brands or clients.

The dashboard lists every configured source when connecting an account, and the
account records which one authorised it (in its ``meta``), so it is always clear
where a connection came from. An earlier version hid the ``.env`` option once a
dashboard app existed; that stranded accounts connected through ``.env`` with no
way to reconnect them.
"""
from __future__ import annotations

from ..config import PlatformCreds, settings
from ..models import PlatformApp, PlatformName

# Pseudo app-id meaning "the credentials in .env", so a connect can ask for them
# explicitly rather than by absence (which would be indistinguishable from "no
# preference" once dashboard apps exist).
ENV_APP_ID = "env"


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

    ``app_id`` may be:

    * a :class:`~aismm.models.PlatformApp` id — use that app;
    * :data:`ENV_APP_ID` — use ``.env`` explicitly, even when apps exist;
    * empty/None — no preference: ``.env`` if configured, else the first app.

    ``.env`` wins the no-preference case because it is the pre-existing setup:
    accounts connected before the Apps page existed came from there, and
    reconnecting one must not silently switch it to a different app.
    """
    if app_id == ENV_APP_ID:
        return env_creds(platform)
    if app_id:
        app = store.get_platform_app(app_id)
        if app and app.platform == platform:
            return app_creds(app, store)

    env = env_creds(platform)
    if env.configured:
        return env
    apps = available_apps(platform, store)
    return app_creds(apps[0], store) if apps else PlatformCreds()


def connection_options(platform: PlatformName, store) -> list[dict]:
    """Every "connect with…" choice the dashboard can offer, ``.env`` included.

    Each entry is ``{app_id, label, configured, is_env}``. Both sources are
    always listed: an account connected through ``.env`` still needs that route
    to reconnect after its token expires, and hiding it once the first dashboard
    app appeared left no way back to it.
    """
    options = []
    env = env_creds(platform)
    if env.configured:
        options.append({"app_id": ENV_APP_ID, "label": "from .env (default)",
                        "configured": True, "is_env": True})
    options.extend(
        {"app_id": app.id, "label": app.label, "configured": bool(app.client_id),
         "is_env": False}
        for app in available_apps(platform, store)
    )
    if not options:                       # nothing configured anywhere
        options.append({"app_id": ENV_APP_ID, "label": "from .env", "configured": False,
                        "is_env": True})
    return options


def is_configured(platform: PlatformName, store) -> bool:
    return any(option["configured"] for option in connection_options(platform, store))
