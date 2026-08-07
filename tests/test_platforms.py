import asyncio

import pytest

from aismm.models import Account, PlatformName
from aismm.platforms.registry import get_platform, registered_platforms


def test_all_platforms_registered():
    names = {p.value for p in registered_platforms()}
    assert names == {"instagram", "twitter", "youtube", "tiktok", "linkedin", "facebook"}


def test_capabilities_matrix():
    yt = get_platform(PlatformName.youtube).capabilities
    assert yt.supports_video and not yt.supports_text and not yt.supports_image
    tk = get_platform(PlatformName.tiktok).capabilities
    assert tk.supports_video and not tk.supports_text
    tw = get_platform(PlatformName.twitter).capabilities
    assert tw.supports_text and tw.supports_image and tw.supports_video
    ig = get_platform(PlatformName.instagram).capabilities
    assert ig.needs_public_media_url and ig.supports_image and ig.supports_video
    li = get_platform(PlatformName.linkedin).capabilities
    assert li.supports_text and li.supports_image and li.supports_video
    fb = get_platform(PlatformName.facebook).capabilities
    assert fb.needs_public_media_url and fb.supports_text and fb.supports_carousel


def test_authorize_url_shapes():
    # X uses PKCE + a code_challenge; TikTok uses client_key (not client_id).
    tw_url = get_platform(PlatformName.twitter).authorize_url(
        redirect_uri="https://app/cb", state="s", code_challenge="chal")
    assert "code_challenge=chal" in tw_url and "response_type=code" in tw_url
    tk_url = get_platform(PlatformName.tiktok).authorize_url(
        redirect_uri="https://app/cb", state="s")
    assert "client_key=" in tk_url


def test_instagram_requires_public_url():
    ig = get_platform(PlatformName.instagram)
    acct = Account(platform=PlatformName.instagram, external_id="123")
    # default DASHBOARD_BASE_URL is localhost -> the public-URL guard must fire
    with pytest.raises(RuntimeError):
        asyncio.run(ig.publish(access_token="t", account=acct, caption="c",
                               asset_path="/tmp/x.mp4", media_kind="video"))
