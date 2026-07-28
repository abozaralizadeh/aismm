"""LLM wiring for the OpenAI Agents SDK.

Two providers behind one toggle (``LLM_PROVIDER``):

* ``azure`` — talk directly to Azure OpenAI with an api-key
  (the SandBox/ComicBook pattern, verbatim).
* ``apim``  — talk through an Azure API Management gateway / load balancer.

**Both build an ``AsyncAzureOpenAI`` client** — APIM fronts Azure OpenAI, so it
speaks the Azure URL shape, and the gateway route is just the ``azure_endpoint``
(the trAIde pattern, ``src/agent.py::_build_openai_client``). This matters: a
plain ``AsyncOpenAI(base_url=...)`` omits Azure's ``/openai`` path segment and
posts to ``{apim}/responses`` instead of ``{apim}/openai/responses``, which the
gateway does not route. The Azure client also authenticates with the ``api-key``
header alone, instead of adding a bogus ``Authorization: Bearer``.

Both register the client as the SDK default (so the hosted ``WebSearchTool``
routes through it too) and return one shared ``OpenAIResponsesModel``.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from agents import OpenAIResponsesModel, set_default_openai_client
from openai import AsyncAzureOpenAI

from .config import settings

logger = logging.getLogger("aismm.llm")

_LLM_TIMEOUT = 600.0  # generous; video/image tools have their own timeouts


def _build_azure_client() -> AsyncAzureOpenAI:
    llm = settings.llm
    if not (llm.azure_api_key and llm.azure_endpoint):
        raise RuntimeError(
            "LLM_PROVIDER=azure but AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT are unset."
        )
    return AsyncAzureOpenAI(
        api_key=llm.azure_api_key,
        api_version=llm.azure_api_version,
        azure_endpoint=llm.azure_endpoint,
        azure_deployment=llm.model,
        timeout=_LLM_TIMEOUT,
    )


def _build_apim_client() -> AsyncAzureOpenAI:
    """APIM route → Azure-shaped client (see module docstring for why).

    ``azure_endpoint`` is the APIM route *without* ``/openai``; the SDK appends
    it, producing ``{APIM_BASE_URL}/openai/responses?api-version=…``.
    """
    llm = settings.llm
    if not (llm.apim_base_url and llm.apim_subscription_key):
        raise RuntimeError(
            "LLM_PROVIDER=apim but APIM_BASE_URL / APIM_SUBSCRIPTION_KEY are unset."
        )
    base = llm.apim_base_url.rstrip("/")
    # Tolerate an APIM_BASE_URL that already ends in /openai — the SDK adds it.
    if base.endswith("/openai"):
        base = base[: -len("/openai")]
    # api_key becomes the `api-key` header. Policies that expect a different
    # header (e.g. Ocp-Apim-Subscription-Key) get it added explicitly.
    extra_headers = {}
    if llm.apim_key_header and llm.apim_key_header.lower() != "api-key":
        extra_headers[llm.apim_key_header] = llm.apim_subscription_key
    return AsyncAzureOpenAI(
        api_key=llm.apim_subscription_key,
        api_version=llm.apim_api_version,
        azure_endpoint=base,
        azure_deployment=llm.model,
        default_headers=extra_headers or None,
        timeout=_LLM_TIMEOUT,
    )


@lru_cache(maxsize=1)
def _client():
    provider = settings.llm.provider
    if provider == "apim":
        client = _build_apim_client()
    elif provider == "azure":
        client = _build_azure_client()
    else:
        raise RuntimeError(f"Unknown LLM_PROVIDER={provider!r} (expected 'azure' or 'apim').")
    set_default_openai_client(client)
    # Log where calls actually go — the first thing to check when a gateway
    # 404s or times out. (client.base_url carries a /deployments/<model> suffix
    # that the SDK drops for /responses, so it would read as misleading here.)
    endpoint = (settings.llm.apim_base_url if provider == "apim"
                else settings.llm.azure_endpoint).rstrip("/").removesuffix("/openai")
    logger.info("LLM client ready (provider=%s, model=%s, responses → %s/openai/responses)",
                provider, settings.llm.model, endpoint)
    return client


@lru_cache(maxsize=1)
def build_model() -> OpenAIResponsesModel:
    """Return the shared Responses model used by every agent."""
    return OpenAIResponsesModel(model=settings.llm.model, openai_client=_client())


def configure_tracing() -> None:
    """Point the Agents SDK's tracing somewhere that actually accepts it.

    The SDK uploads traces to ``api.openai.com`` using the **default client's**
    key. On Azure/APIM that key is not a platform.openai.com key, so every run
    logs ``Tracing client error 401: Incorrect API key provided`` — noise that
    looks like a real failure but isn't. So:

    * ``LANGCHAIN_API_KEY`` set → send traces to LangSmith instead;
    * else ``OPENAI_API_KEY`` set → leave the built-in exporter alone (that key
      is a real platform key, so the upload will work);
    * else → disable tracing.

    Never fatal: tracing is best-effort.
    """
    import os

    if os.getenv("LANGCHAIN_API_KEY"):
        try:
            from agents import set_trace_processors
            from langsmith.wrappers import OpenAIAgentsTracingProcessor

            set_trace_processors([OpenAIAgentsTracingProcessor()])
            logger.info("LangSmith tracing enabled.")
            return
        except Exception as exc:  # pragma: no cover - tracing is best-effort
            logger.warning("LangSmith tracing not enabled: %s", exc)

    if os.getenv("OPENAI_API_KEY"):
        return

    try:
        from agents import set_tracing_disabled

        set_tracing_disabled(True)
        logger.info("Agent tracing disabled (no LANGCHAIN_API_KEY / OPENAI_API_KEY).")
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not disable agent tracing: %s", exc)
