"""Where to get each platform's OAuth credentials, shown in the dashboard.

Creating a developer app is the fiddliest part of setting AISMM up, and every
platform hides the values somewhere different — Meta's *Instagram* app ID is not
the one the login dialog wants, TikTok calls its client id a "client key". These
notes render on the Apps page next to the form, so the steps sit beside the boxes
being filled in rather than in a README nobody has open.

Content only: no imports beyond the enum, so it stays cheap to keep current.
"""
from __future__ import annotations

from ..models import PlatformName

GUIDES: dict[str, dict] = {
    "instagram": {
        "title": "Instagram (via a Meta app)",
        "console": "https://developers.facebook.com/apps",
        "console_label": "Meta for Developers → My Apps",
        "docs": "https://developers.facebook.com/docs/instagram-platform/content-publishing",
        "requires": "An Instagram **Business or Creator** account linked to a Facebook Page.",
        "id_label": "App ID",
        "secret_label": "App secret",
        "steps": [
            "Create an app of type **Business** in Meta for Developers.",
            "Add the **Instagram** product, then **Facebook Login for Business**.",
            "Copy the credentials from **App settings → Basic** — the *App ID* and "
            "*App secret* at the top of that page.",
            "⚠️ Do NOT use the *Instagram app ID* shown on the Instagram product page. "
            "It belongs to a different login flow and the dialog will reject it with "
            "\"Invalid App ID\".",
            "Under **Facebook Login → Settings**, add the redirect URI below to "
            "*Valid OAuth Redirect URIs*.",
            "Request the permissions `instagram_basic`, `instagram_content_publish`, "
            "`pages_show_list` and `pages_read_engagement`. Publishing to accounts you "
            "don't own needs App Review.",
            "⚠️ Meta rejects the *entire* login dialog if your app cannot request even "
            "one scope — \"Invalid Scopes: …\". `instagram_manage_insights` needs App "
            "Review; if the dialog refuses, drop it from `INSTAGRAM_SCOPES` in your .env "
            "and add it back once it is approved.",
            "⚠️ **Several Instagram accounts? Tick EVERY Page in ONE login.** A single "
            "Connect claims every Page the login administers, so they all arrive at once. "
            "Doing them one at a time breaks the earlier ones: this app holds a single "
            "grant per Facebook login, and authorising again REPLACES it — the accounts "
            "already connected then fail with \"must be granted before impersonating a "
            "user's page\" while the newest one works fine.",
        ],
        "notes": "Instagram fetches media from a public URL, so this deployment must be "
                 "reachable from the internet (or use Azure Blob storage). If a connect "
                 "fails with \"Invalid Scopes\", set INSTAGRAM_SCOPES to just the "
                 "permissions your app is actually approved for.",
    },
    "twitter": {
        "title": "X (Twitter)",
        "console": "https://developer.x.com/en/portal/dashboard",
        "console_label": "X Developer Portal → Projects & Apps",
        "docs": "https://docs.x.com/x-api/posts/creation-of-a-post",
        "requires": "A project + app on a plan that allows posting (Basic or above).",
        "id_label": "OAuth 2.0 Client ID",
        "secret_label": "OAuth 2.0 Client Secret",
        "steps": [
            "Create a project and an app in the X Developer Portal.",
            "Open **User authentication settings** and turn on **OAuth 2.0**.",
            "Set the app type to **Web App / Automated App or Bot** (confidential client).",
            "Add the callback URI below, and any website URL.",
            "Copy the **OAuth 2.0 Client ID and Client Secret** — not the API key/secret.",
            "Scopes used: `tweet.read tweet.write users.read media.write offline.access`.",
        ],
        "extra_fields": [
            {"key": "api_key", "label": "API key (optional)",
             "help": "OAuth 1.0a consumer key — only needed for the legacy v1.1 media upload."},
            {"key": "api_secret", "label": "API secret (optional)", "help": ""},
        ],
    },
    "youtube": {
        "title": "YouTube (Google Cloud)",
        "console": "https://console.cloud.google.com/apis/credentials",
        "console_label": "Google Cloud Console → APIs & Services → Credentials",
        "docs": "https://developers.google.com/youtube/v3/guides/uploading_a_video",
        "requires": "A Google Cloud project with the **YouTube Data API v3** enabled.",
        "id_label": "Client ID",
        "secret_label": "Client secret",
        "steps": [
            "Enable **YouTube Data API v3** for your project.",
            "Configure the **OAuth consent screen** (External is fine; add yourself as a "
            "test user while it is unverified).",
            "Create credentials → **OAuth client ID** → application type **Web application**.",
            "Add the redirect URI below to *Authorized redirect URIs*.",
            "Copy the generated Client ID and Client secret.",
        ],
        "notes": "Each upload costs ~1600 quota units of a 10,000/day default — roughly six "
                 "uploads a day until you request more.",
    },
    "tiktok": {
        "title": "TikTok",
        "console": "https://developers.tiktok.com/apps",
        "console_label": "TikTok for Developers → Manage apps",
        "docs": "https://developers.tiktok.com/doc/content-posting-api-get-started",
        "requires": "An app with the **Content Posting API** product added.",
        "id_label": "Client key",
        "secret_label": "Client secret",
        "steps": [
            "Create an app and add the **Content Posting API** product.",
            "Enable **Direct Post** if you want AISMM to publish rather than draft.",
            "Add the redirect URI below to the app's redirect URIs.",
            "Copy the **Client key** (this is the client id) and **Client secret**.",
            "Scopes used: `user.info.basic`, `video.publish`, `video.upload`.",
        ],
        "notes": "Until the app passes TikTok's audit, every post is forced to "
                 "`SELF_ONLY` visibility — visible to you alone.",
    },
}


def guide_for(platform: PlatformName) -> dict:
    """Setup notes for a platform, or a minimal placeholder for a new one."""
    return GUIDES.get(platform.value, {
        "title": platform.value.title(),
        "console": "", "console_label": "", "docs": "", "requires": "",
        "id_label": "Client ID", "secret_label": "Client secret",
        "steps": ["Create an OAuth app on the platform and paste its credentials here."],
    })
