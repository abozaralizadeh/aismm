"""describe_model_error turns an LLM-provider failure into a readable run message.

The live bug: an Azure/APIM 500 came back as a full HTML error page, and that whole
blob landed verbatim in Run.error and the console ("openai.InternalServerError:
<html>…500 - The request timed out.…"), which told the operator nothing.
"""
import httpx
import openai

from aismm.llm import describe_model_error

_REQ = httpx.Request("POST", "https://apim.example/openai/responses")
_HTML_500 = ("<html><head><title>500 - The request timed out.</title></head>"
             "<body><font color=\"#aa0000\">timeout</font></body></html>")


def test_500_is_readable_and_strips_html():
    resp = httpx.Response(500, request=_REQ, text=_HTML_500)
    msg = describe_model_error(openai.InternalServerError(_HTML_500, response=resp, body=None))
    assert "<" not in msg and ">" not in msg          # no raw markup leaks through
    assert "HTTP 500" in msg
    assert "retry" in msg.lower()                      # says a retry is the fix
    assert "timed out" in msg.lower()                  # the useful bit of the body survives


def test_timeout_and_connection_are_transient():
    assert "timed out" in describe_model_error(
        openai.APITimeoutError(request=_REQ)).lower()
    conn = describe_model_error(openai.APIConnectionError(request=_REQ))
    assert "reach" in conn.lower()


def test_auth_and_rate_limit_named_distinctly():
    resp401 = httpx.Response(401, request=_REQ, text="bad key")
    assert "401" in describe_model_error(
        openai.AuthenticationError("x", response=resp401, body=None))
    resp429 = httpx.Response(429, request=_REQ, text="slow down")
    assert "429" in describe_model_error(
        openai.RateLimitError("x", response=resp429, body=None))


def test_non_model_error_returns_none():
    # A plain error falls through to the caller's str(exc) fallback.
    assert describe_model_error(ValueError("something else")) is None
    assert describe_model_error(RuntimeError("media missing")) is None


def test_body_is_capped():
    resp = httpx.Response(503, request=_REQ, text="x" * 5000)
    msg = describe_model_error(openai.InternalServerError("x", response=resp, body=None))
    assert len(msg) < 500 and msg.endswith("…")
