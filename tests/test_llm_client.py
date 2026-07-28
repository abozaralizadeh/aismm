"""LLM client wiring — the URL an agent call actually goes to.

The APIM route is the interesting one: a plain ``AsyncOpenAI(base_url=…)`` drops
Azure's ``/openai`` path segment and posts to ``{apim}/responses``, which the
gateway does not route. These tests pin the real request URL (via a mocked
transport — no network) so that regression can't come back silently.
"""
import dataclasses

import httpx
import pytest

from aismm import config as config_module
from aismm import llm as llm_module
from aismm.config import LLMSettings

APIM = "https://gateway.azure-api.net/openailb"
AZURE = "https://my-resource.openai.azure.com"
MODEL = "gpt-4o"
VERSION = "2025-04-01-preview"


@pytest.fixture(autouse=True)
def _clear_caches():
    llm_module._client.cache_clear()
    llm_module.build_model.cache_clear()
    yield
    llm_module._client.cache_clear()
    llm_module.build_model.cache_clear()


def _with_llm(monkeypatch, **llm_kwargs):
    llm = LLMSettings(model=MODEL, **llm_kwargs)
    monkeypatch.setattr(llm_module, "settings",
                        dataclasses.replace(config_module.settings, llm=llm))


def _capture_request(client) -> httpx.Request:
    """Issue one responses.create through a mock transport and return the request."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={
            "id": "resp_1", "object": "response", "created_at": 0, "model": MODEL,
            "output": [], "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
        })

    import asyncio
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(client.responses.create(model=MODEL, input="ping"))
    return captured["request"]


# --- APIM --------------------------------------------------------------------- #

def test_apim_request_includes_the_openai_path_segment(monkeypatch):
    _with_llm(monkeypatch, provider="apim", apim_base_url=APIM,
              apim_subscription_key="subkey", apim_api_version=VERSION)
    request = _capture_request(llm_module._build_apim_client())

    assert request.url.path == "/openailb/openai/responses", (
        "APIM must be called at {base}/openai/responses — without the /openai "
        "segment the gateway does not route the request")
    assert request.url.params["api-version"] == VERSION


def test_apim_authenticates_with_the_api_key_header_only(monkeypatch):
    """The Azure client must not add a bogus `Authorization: Bearer <subscription key>`."""
    _with_llm(monkeypatch, provider="apim", apim_base_url=APIM,
              apim_subscription_key="subkey", apim_api_version=VERSION)
    request = _capture_request(llm_module._build_apim_client())

    assert request.headers.get("api-key") == "subkey"
    assert "authorization" not in request.headers


def test_apim_supports_a_custom_subscription_key_header(monkeypatch):
    _with_llm(monkeypatch, provider="apim", apim_base_url=APIM,
              apim_subscription_key="subkey", apim_api_version=VERSION,
              apim_key_header="Ocp-Apim-Subscription-Key")
    request = _capture_request(llm_module._build_apim_client())

    assert request.headers.get("Ocp-Apim-Subscription-Key") == "subkey"


def test_apim_base_url_already_ending_in_openai_is_not_doubled(monkeypatch):
    _with_llm(monkeypatch, provider="apim", apim_base_url=f"{APIM}/openai/",
              apim_subscription_key="subkey", apim_api_version=VERSION)
    request = _capture_request(llm_module._build_apim_client())

    assert request.url.path == "/openailb/openai/responses"


def test_apim_requires_its_credentials(monkeypatch):
    _with_llm(monkeypatch, provider="apim", apim_base_url="", apim_subscription_key="")
    with pytest.raises(RuntimeError, match="APIM_BASE_URL"):
        llm_module._build_apim_client()


# --- Azure direct -------------------------------------------------------------- #

def test_azure_direct_request_url(monkeypatch):
    _with_llm(monkeypatch, provider="azure", azure_api_key="k",
              azure_endpoint=AZURE, azure_api_version=VERSION)
    request = _capture_request(llm_module._build_azure_client())

    assert request.url.path == "/openai/responses"
    assert request.headers.get("api-key") == "k"


def test_azure_requires_its_credentials(monkeypatch):
    _with_llm(monkeypatch, provider="azure", azure_api_key="", azure_endpoint="")
    with pytest.raises(RuntimeError, match="AZURE_OPENAI_API_KEY"):
        llm_module._build_azure_client()


def test_unknown_provider_is_rejected(monkeypatch):
    _with_llm(monkeypatch, provider="bogus")
    with pytest.raises(RuntimeError, match="Unknown LLM_PROVIDER"):
        llm_module._client()


# --- tracing ------------------------------------------------------------------- #

def test_tracing_is_disabled_without_a_platform_key(monkeypatch):
    """Otherwise the SDK uploads traces to api.openai.com with the Azure key -> 401 spam."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr("agents.set_tracing_disabled", lambda flag: calls.append(flag))

    llm_module.configure_tracing()
    assert calls == [True]


def test_tracing_left_alone_with_a_real_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr("agents.set_tracing_disabled", lambda flag: calls.append(flag))

    llm_module.configure_tracing()
    assert calls == []
