"""LLM wiring for the OpenAI Agents SDK.

Two providers behind one toggle (``LLM_PROVIDER``):

* ``azure`` — talk directly to Azure OpenAI with an api-key
  (the SandBox/ComicBook pattern, verbatim).
* ``apim``  — talk through an Azure API Management gateway / load balancer:
  an ``AsyncOpenAI`` client whose ``base_url`` is the APIM route and whose
  subscription key rides in a configurable header (``api-key`` or
  ``Ocp-Apim-Subscription-Key``, per your APIM policy), with ``api-version``
  pinned as a default query param.

Both register the client as the SDK default (so the hosted ``WebSearchTool``
routes through it too) and return one shared ``OpenAIResponsesModel``.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from agents import OpenAIResponsesModel, set_default_openai_client
from openai import AsyncAzureOpenAI, AsyncOpenAI

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


def _build_apim_client() -> AsyncOpenAI:
    llm = settings.llm
    if not (llm.apim_base_url and llm.apim_subscription_key):
        raise RuntimeError(
            "LLM_PROVIDER=apim but APIM_BASE_URL / APIM_SUBSCRIPTION_KEY are unset."
        )
    return AsyncOpenAI(
        base_url=llm.apim_base_url.rstrip("/"),
        # APIM authenticates via the subscription-key header; api_key is required
        # by the SDK constructor, so we reuse the same value harmlessly.
        api_key=llm.apim_subscription_key,
        default_headers={llm.apim_key_header: llm.apim_subscription_key},
        default_query={"api-version": llm.apim_api_version},
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
    logger.info("LLM client ready (provider=%s, model=%s)", provider, settings.llm.model)
    return client


@lru_cache(maxsize=1)
def build_model() -> OpenAIResponsesModel:
    """Return the shared Responses model used by every agent."""
    return OpenAIResponsesModel(model=settings.llm.model, openai_client=_client())


def configure_tracing() -> None:
    """Enable Agents-SDK -> LangSmith tracing if LANGCHAIN_* env is present.

    No-op (and never fatal) if langsmith isn't installed or env isn't set.
    """
    import os

    if not os.getenv("LANGCHAIN_API_KEY"):
        return
    try:
        from agents import set_trace_processors
        from langsmith.wrappers import OpenAIAgentsTracingProcessor

        set_trace_processors([OpenAIAgentsTracingProcessor()])
        logger.info("LangSmith tracing enabled.")
    except Exception as exc:  # pragma: no cover - tracing is best-effort
        logger.warning("LangSmith tracing not enabled: %s", exc)
