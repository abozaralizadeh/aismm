import asyncio

import httpx
import pytest

from aismm.models import Account, PlatformName
from aismm.platforms.registry import get_platform, registered_platforms
from aismm.platforms.tiktok import TikTok


def _tk_response(status, body):
    return httpx.Response(status, json=body,
                          request=httpx.Request("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/"))


def test_tiktok_403_explains_it_is_permission_not_the_video():
    """A bare 'Client error 403 Forbidden' told the agent nothing; the reason
    (unaudited app / missing scope) must be spelled out so it does not
    regenerate the clip chasing the wrong cause."""
    resp = _tk_response(403, {"error": {"code": "access_denied",
                                        "message": "Forbidden", "log_id": "abc123"}})
    err = TikTok._api_error(resp)
    msg = str(err)
    assert "403" in msg
    assert "not a problem with the video" in msg.lower() or "not about the video" in msg.lower() \
        or "regenerat" in msg.lower()
    assert "abc123" in msg  # the log_id TikTok support can trace


def test_tiktok_scope_error_names_video_publish_and_reconnect():
    resp = _tk_response(403, {"error": {"code": "scope_not_authorized",
                                        "message": "scope not authorized"}})
    msg = str(TikTok._api_error(resp))
    assert "video.publish" in msg and "reconnect" in msg.lower()


def test_tiktok_check_raises_on_error_code_in_a_200_body():
    """TikTok signals failure with error.code even on a 2xx; raise_for_status
    misses that, so _check must catch it."""
    ok = _tk_response(200, {"error": {"code": "ok"}, "data": {"publish_id": "p"}})
    assert TikTok._check(TikTok, ok)["data"]["publish_id"] == "p"
    bad = _tk_response(200, {"error": {"code": "spam_risk_too_many_posts",
                                       "message": "too many"}})
    with pytest.raises(RuntimeError):
        TikTok._check(TikTok, bad)


def test_all_platforms_registered():
    names = {p.value for p in registered_platforms()}
    assert names == {"instagram", "twitter", "youtube", "tiktok", "linkedin", "facebook",
                     "reddit"}


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
    rd = get_platform(PlatformName.reddit).capabilities
    assert rd.supports_text and rd.supports_image and not rd.supports_video


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
