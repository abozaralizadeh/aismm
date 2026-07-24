"""Instagram — Meta Instagram Graph API (content publishing).

Requires an Instagram **Business/Creator** account linked to a Facebook Page, and
a Meta app with ``instagram_content_publish``. Publishing is a two-step Graph call
and, crucially, Instagram FETCHES the media from a PUBLIC URL — so the generated
asset must be reachable (the dashboard serves it at ``/assets/<file>``; set
``DASHBOARD_BASE_URL`` to a public https URL, e.g. an ngrok tunnel, when testing).

Flow:
    1. POST /{ig-user-id}/media   (image_url | video_url + media_type=REELS)  -> creation_id
    2. GET  /{creation_id}?fields=status_code  until FINISHED   (videos/reels)
    3. POST /{ig-user-id}/media_publish  (creation_id)          -> media_id
    4. GET  /{media_id}?fields=permalink                        -> permalink
"""
from __future__ import annotations

import asyncio

import httpx

from ..assets import public_url
from ..models import Account, PlatformName
from .base import Capabilities, Identity, PublishResult, SocialPlatform
from .registry import register

GRAPH_VERSION = "v21.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"


class Instagram(SocialPlatform):
    name = PlatformName.instagram
    capabilities = Capabilities(
        supports_text=False,           # IG posts always carry media
        supports_image=True,
        supports_video=True,           # Reels
        needs_public_media_url=True,
        default_orientation="portrait",
        caption_limit=2200,
        notes="Business/Creator account required. Reels: 9:16, 5–90s. Media must be at a public URL.",
    )
    auth_endpoint = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
    token_endpoint = f"{GRAPH}/oauth/access_token"
    scopes = [
        "instagram_basic",
        "instagram_content_publish",
        "pages_show_list",
        "pages_read_engagement",
        "business_management",
    ]
    scope_sep = ","

    async def fetch_identity(self, access_token: str) -> Identity:
        """Resolve the IG business account + its Page access token from a user token."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{GRAPH}/me/accounts",
                params={"fields": "name,access_token,instagram_business_account{id,username}",
                        "access_token": access_token},
            )
            resp.raise_for_status()
            pages = resp.json().get("data", [])
        for page in pages:
            iba = page.get("instagram_business_account")
            if iba:
                return Identity(
                    external_id=iba["id"],
                    handle=iba.get("username") or page.get("name", ""),
                    # Publishing uses the PAGE access token; store it as the account token.
                    meta={"access_token": page.get("access_token", access_token),
                          "page_name": page.get("name", "")},
                )
        raise RuntimeError(
            "No Instagram Business account found on any Facebook Page for this login. "
            "Link an IG Business/Creator account to a Page and grant instagram_content_publish."
        )

    async def _wait_finished(self, client, creation_id, token, tries=10, delay=6.0):
        for _ in range(tries):
            r = await client.get(f"{GRAPH}/{creation_id}",
                                 params={"fields": "status_code", "access_token": token})
            r.raise_for_status()
            status = r.json().get("status_code")
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                raise RuntimeError(f"Instagram media container {creation_id} status={status}")
            await asyncio.sleep(delay)
        raise TimeoutError(f"Instagram container {creation_id} not FINISHED in time")

    async def publish(self, *, access_token, account: Account, caption, asset_path, media_kind) -> PublishResult:
        ig_user_id = account.external_id
        media_url = public_url(asset_path)
        if not media_url:
            raise RuntimeError("Instagram needs a media asset; generate an image or reel first.")
        if media_url.startswith(("http://127.", "http://localhost")):
            raise RuntimeError(
                "Instagram must fetch media from a PUBLIC url. Set DASHBOARD_BASE_URL to a "
                "public https address (e.g. an ngrok tunnel) so /assets/<file> is reachable."
            )
        async with httpx.AsyncClient(timeout=120) as client:
            create_params = {"caption": caption, "access_token": access_token}
            if media_kind == "video":
                create_params.update({"media_type": "REELS", "video_url": media_url})
            else:
                create_params["image_url"] = media_url
            r = await client.post(f"{GRAPH}/{ig_user_id}/media", params=create_params)
            r.raise_for_status()
            creation_id = r.json()["id"]

            # Reels/videos need processing time; images are usually immediate.
            if media_kind == "video":
                await self._wait_finished(client, creation_id, access_token)

            r = await client.post(f"{GRAPH}/{ig_user_id}/media_publish",
                                  params={"creation_id": creation_id, "access_token": access_token})
            r.raise_for_status()
            media_id = r.json()["id"]

            perma = await client.get(f"{GRAPH}/{media_id}",
                                     params={"fields": "permalink", "access_token": access_token})
            url = perma.json().get("permalink", "") if perma.status_code == 200 else ""
        return PublishResult(url=url, external_id=media_id, raw={"media_id": media_id})


register(PlatformName.instagram, Instagram)
