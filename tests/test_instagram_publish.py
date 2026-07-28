"""Instagram publishing: error reporting, token handling, container readiness.

All Graph traffic is mocked — these pin the behaviour that made a live 400
undiagnosable: no error body, and the access token sitting in the request URL
(and therefore in the exception message, and therefore in the service log).
"""
import asyncio
import json

import httpx
import pytest

from aismm.models import Account, PlatformName
from aismm.platforms import instagram as ig

TOKEN = "EAGsecret-page-token-value"
IG_USER = "17841413934356307"


@pytest.fixture()
def account():
    return Account(platform=PlatformName.instagram, handle="tester", external_id=IG_USER)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Skip the real backoff. Bind the true sleep first — `ig.asyncio` *is* the
    asyncio module, so a lambda calling asyncio.sleep would call itself."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(ig.asyncio, "sleep", lambda _s: real_sleep(0))
    monkeypatch.setattr(ig, "_PUBLISH_RETRY_DELAY", 0)


def _run(account, monkeypatch, handler, media_kind="image"):
    """Drive publish() against a mocked Graph, returning (result, requests)."""
    requests: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(_record)
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(ig.httpx, "AsyncClient", _client)
    monkeypatch.setattr(ig, "public_url", lambda p: "https://public.example.com/assets/a.mp4")

    coro = ig.Instagram(creds=None).publish(
        access_token=TOKEN, account=account, caption="hi",
        asset_path="/tmp/a.mp4", media_kind=media_kind)
    try:
        return asyncio.run(coro), requests
    except Exception as exc:  # returned so tests can assert on the message
        return exc, requests


def _graph_error_response(request, status=400, **error):
    payload = {"error": {"message": "Media ID is not available", "type": "OAuthException",
                         "code": 9007, "fbtrace_id": "AbCdEf", **error}}
    return httpx.Response(status, request=request, json=payload)


def _happy(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/media"):
        return httpx.Response(200, request=request, json={"id": "container-1"})
    if path.endswith("/media_publish"):
        return httpx.Response(200, request=request, json={"id": "media-9"})
    if "container-1" in path:
        return httpx.Response(200, request=request, json={"status_code": "FINISHED"})
    return httpx.Response(200, request=request, json={"permalink": "https://instagram.com/p/x"})


# --- the token must never reach a URL ----------------------------------------- #

def test_token_is_sent_as_a_bearer_header_not_in_the_url(account, monkeypatch):
    result, requests = _run(account, monkeypatch, _happy)

    assert not isinstance(result, Exception)
    assert requests, "no Graph calls were made"
    for request in requests:
        assert TOKEN not in str(request.url), f"token leaked into URL: {request.url}"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_error_message_never_contains_the_token(account, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/media"):
            return _graph_error_response(request, message="Bad caption", code=100)
        return _happy(request)

    result, _ = _run(account, monkeypatch, handler)
    assert isinstance(result, RuntimeError)
    assert TOKEN not in str(result)


# --- Graph's error body must be surfaced -------------------------------------- #

def test_publish_failure_reports_graph_message_and_codes(account, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/media_publish"):
            return _graph_error_response(
                request, message="The media is not eligible", code=352,
                error_subcode=2207026, error_user_msg="Video is too long")
        return _happy(request)

    result, _ = _run(account, monkeypatch, handler)
    assert isinstance(result, RuntimeError)
    text = str(result)
    assert "The media is not eligible" in text      # the actual reason
    assert "code=352" in text and "error_subcode=2207026" in text
    assert "Video is too long" in text
    assert "fbtrace_id=AbCdEf" in text              # what Meta support asks for


def test_non_json_error_body_still_reported(account, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/media"):
            return httpx.Response(500, request=request, text="<html>gateway</html>")
        return _happy(request)

    result, _ = _run(account, monkeypatch, handler)
    assert isinstance(result, RuntimeError)
    assert "500" in str(result) and "gateway" in str(result)


# --- container readiness ------------------------------------------------------- #

def test_publish_retries_while_media_is_not_yet_available(account, monkeypatch):
    """Graph answers a too-early publish with code 9007; that must not fail the post."""
    attempts = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/media_publish"):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return _graph_error_response(request)      # code 9007
            return httpx.Response(200, request=request, json={"id": "media-9"})
        return _happy(request)

    result, _ = _run(account, monkeypatch, handler)
    assert not isinstance(result, Exception)
    assert result.external_id == "media-9"
    assert attempts["n"] == 3


def test_images_also_wait_for_the_container(account, monkeypatch):
    """An image container that isn't FINISHED yet must be waited for, not published."""
    polls = {"n": 0}

    def handler(request):
        if "container-1" in request.url.path and not request.url.path.endswith("media_publish"):
            polls["n"] += 1
            status = "FINISHED" if polls["n"] >= 2 else "IN_PROGRESS"
            return httpx.Response(200, request=request, json={"status_code": status})
        return _happy(request)

    result, _ = _run(account, monkeypatch, handler, media_kind="image")
    assert not isinstance(result, Exception)
    assert polls["n"] >= 2


def test_container_error_status_is_reported(account, monkeypatch):
    def handler(request):
        if "container-1" in request.url.path:
            return httpx.Response(200, request=request,
                                  json={"status_code": "ERROR", "status": "Media download failed"})
        return _happy(request)

    result, _ = _run(account, monkeypatch, handler)
    assert isinstance(result, RuntimeError)
    assert "Media download failed" in str(result)


def test_successful_publish_returns_the_permalink(account, monkeypatch):
    result, _ = _run(account, monkeypatch, _happy)
    assert result.url == "https://instagram.com/p/x"
    assert result.external_id == "media-9"


# --- error formatting unit ------------------------------------------------------ #

def test_safe_url_strips_the_query_string():
    request = httpx.Request("GET", "https://graph.facebook.com/v21.0/me?access_token=secret")
    response = httpx.Response(400, request=request, json={})
    assert ig._safe_url(response) == "https://graph.facebook.com/v21.0/me"
    assert "secret" not in ig._safe_url(response)


def test_graph_error_returns_the_error_dict():
    request = httpx.Request("POST", "https://graph.facebook.com/v21.0/x/media_publish")
    response = httpx.Response(400, request=request,
                              content=json.dumps({"error": {"message": "nope", "code": 9007}}),
                              headers={"content-type": "application/json"})
    message, err = ig._graph_error(httpx.HTTPStatusError("x", request=request, response=response))
    assert err["code"] == 9007
    assert "nope" in message
