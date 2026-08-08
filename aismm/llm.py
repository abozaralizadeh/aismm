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
import re
from functools import lru_cache

import openai
from agents import OpenAIResponsesModel, set_default_openai_client
from openai import AsyncAzureOpenAI

from .config import settings

logger = logging.getLogger("aismm.llm")

_LLM_TIMEOUT = 600.0  # generous; video/image tools have their own timeouts

_PROVIDER = "the AI model provider (Azure OpenAI / APIM)"
_5XX = {500: "Internal Server Error", 502: "Bad Gateway",
        503: "Service Unavailable", 504: "Gateway Timeout"}
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_body(exc: object) -> str:
    """A short, tag-stripped snippet of an API error body.

    Azure/APIM return a full HTML 500 page on a timeout ("<html>…500 - The
    request timed out.…"); that whole blob otherwise lands verbatim in
    ``Run.error`` and the console, which is what made the failure unreadable.
    Strip the markup, collapse whitespace, and cap the length.
    """
    text = ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            text = resp.text or ""
        except Exception:  # noqa: BLE001 - body may be gone/undecodable
            text = ""
    if not text:
        text = getattr(exc, "message", "") or str(exc)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:200] + "…") if len(text) > 200 else text


def describe_model_error(exc: BaseException) -> str | None:
    """Human-readable summary of a failure from the LLM provider.

    Returns ``None`` when ``exc`` is not a recognizable model/transport error, so
    the caller can fall back to ``str(exc)``. The point is to record WHAT went
    wrong and WHETHER retrying helps — a 5xx/timeout is transient and upstream,
    not something the run's content caused — instead of a raw HTML error page.
    """
    if isinstance(exc, openai.APITimeoutError):
        return (f"{_PROVIDER} timed out before responding. This is a transient upstream "
                "problem, not your instruction or content — retry the run.")
    if isinstance(exc, openai.APIConnectionError):
        return (f"Could not reach {_PROVIDER} (network/connection error): {exc}. "
                "Check connectivity to the endpoint and retry the run.")
    if isinstance(exc, openai.APIStatusError):
        code = getattr(exc, "status_code", None)
        body = _clean_body(exc)
        tail = f" Provider said: {body}" if body else ""
        if code == 429:
            return (f"{_PROVIDER} rate-limited this run (HTTP 429). Wait for the quota to "
                    f"reset, then retry.{tail}")
        if code in _5XX:
            return (f"{_PROVIDER} returned HTTP {code} ({_5XX[code]}). This is a transient "
                    f"failure on their side — retry the run; regenerating media or editing "
                    f"the instruction will not help.{tail}")
        if code == 401:
            return (f"{_PROVIDER} rejected the credentials (HTTP 401). Check the API key / "
                    f"APIM subscription key and the deployment name.{tail}")
        if code == 403:
            return (f"{_PROVIDER} denied access (HTTP 403). The key may lack access to this "
                    f"deployment/region.{tail}")
        if code == 404:
            return (f"{_PROVIDER} returned HTTP 404 — the model deployment name is likely "
                    f"wrong for this endpoint.{tail}")
        if code == 400:
            return (f"{_PROVIDER} rejected the request as invalid (HTTP 400).{tail}")
        return f"{_PROVIDER} returned HTTP {code}.{tail}"
    if isinstance(exc, openai.APIError):
        # Base class for anything above we didn't special-case (e.g. malformed
        # streaming response). Still clearly a provider-side problem.
        return f"{_PROVIDER} returned an error: {_clean_body(exc) or exc}"
    return None


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

    # LANGSMITH_* are the current names; LANGCHAIN_* are the legacy ones SandBox
    # uses. Accept either.
    if os.getenv("LANGCHAIN_API_KEY") or os.getenv("LANGSMITH_API_KEY"):
        try:
            from agents import set_trace_processors
            from langsmith.wrappers import OpenAIAgentsTracingProcessor

            # Route the SDK's own tracer into LangSmith — this is what produces the
            # agent/tool/handoff span tree. Do NOT also wrap the client with
            # `wrap_openai` or add `@traceable` around the same calls: SandBox
            # learned that duplicates traces and flattens the structure.
            set_trace_processors([OpenAIAgentsTracingProcessor()])
            logger.info("LangSmith tracing enabled (project=%s).",
                        os.getenv("LANGCHAIN_PROJECT") or os.getenv("LANGSMITH_PROJECT") or "default")
            return
        except ImportError:
            logger.warning("LANGCHAIN_API_KEY is set but langsmith is not installed — no traces "
                           "will be sent. Fix with: pip install -r requirements.txt")
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
