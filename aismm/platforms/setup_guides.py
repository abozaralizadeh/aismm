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
    "linkedin": {
        "title": "LinkedIn",
        "console": "https://www.linkedin.com/developers/apps",
        "console_label": "LinkedIn Developers → My apps",
        "docs": "https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api",
        "requires": "A LinkedIn app associated with a Company Page (required to create an app).",
        "id_label": "Client ID",
        "secret_label": "Client secret",
        "steps": [
            "Create an app in LinkedIn Developers and verify it against a Company Page.",
            "On the **Products** tab, request **Sign In with LinkedIn using OpenID "
            "Connect** and **Share on LinkedIn**.",
            "On **Auth**, add the redirect URI below to *Authorized redirect URLs*.",
            "Copy the **Client ID** and **Client secret** from the Auth tab.",
            "Scopes used: `openid profile email w_member_social` — posts go to the "
            "signed-in member's feed.",
        ],
        "notes": "Posts as the member who signs in. Company Page posting would need "
                 "`w_organization_social` and an admin role — not enabled here yet.",
    },
    "facebook": {
        "title": "Facebook Pages (via a Meta app)",
        "console": "https://developers.facebook.com/apps",
        "console_label": "Meta for Developers → My Apps",
        "docs": "https://developers.facebook.com/docs/pages-api/posts",
        "requires": "A Facebook **Page** you administer. Reuses the same Meta app as Instagram.",
        "id_label": "App ID",
        "secret_label": "App secret",
        "steps": [
            "Use your existing Meta app (the same one Instagram uses) or create a "
            "**Business** app.",
            "Add **Facebook Login for Business**, and under its settings add the "
            "redirect URI below to *Valid OAuth Redirect URIs*.",
            "Copy the *App ID* and *App secret* from **App settings → Basic**.",
            "Request `pages_show_list`, `pages_read_engagement` and "
            "`pages_manage_posts`. Posting to Pages you don't own needs App Review.",
            "⚠️ **Several Pages? Tick EVERY Page in ONE login** — a single Connect "
            "claims them all. Connecting one at a time replaces the earlier grant, "
            "same as Instagram.",
        ],
        "notes": "Facebook fetches media from a public URL, so this deployment must be "
                 "reachable from the internet (or use Azure Blob storage) — the same "
                 "requirement as Instagram. If FACEBOOK_APP_ID is unset it falls back "
                 "to INSTAGRAM_APP_ID, so an existing Meta app works with no new env vars.",
    },
    "reddit": {
        "title": "Reddit",
        "console": "https://www.reddit.com/prefs/apps",
        "console_label": "Reddit → Preferences → Apps",
        "docs": "https://github.com/reddit-archive/reddit/wiki/OAuth2",
        "requires": "A Reddit account. Posting is subject to each subreddit's rules and "
                    "your account's karma/age.",
        "id_label": "Client ID",
        "secret_label": "Client secret",
        "steps": [
            "Open **Reddit → Preferences → Apps** and click **create another app…**.",
            "Choose type **web app**.",
            "Set the **redirect uri** to the callback URL below (exactly).",
            "Create it, then copy the **client id** (the string under the app name) "
            "and the **secret**.",
            "Scopes used: `identity submit read`. AISMM requests `duration=permanent` "
            "so it can keep the connection alive.",
        ],
        "notes": "A post goes to a subreddit and needs a title — the caption's first line "
                 "becomes the title, the rest the body. Set the destination subreddit per "
                 "account on the Accounts page; with none set it posts to your own "
                 "profile (u_username). Self/text and single-image posts are supported.",
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
