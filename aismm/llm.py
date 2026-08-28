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

import hashlib
import logging
import re
import threading
from functools import lru_cache

import openai
from agents import (ModelSettings, OpenAIResponsesModel, RunConfig,
                    set_default_openai_client)
from openai import AsyncAzureOpenAI

from .config import LLMSettings, settings

logger = logging.getLogger("aismm.llm")

# Every Runner.run passes this. The Responses API is STATEFUL: with the default
# ``store=true`` each turn's output items come back with ids minted by the resource that
# served it (``fc_*`` tool calls, ``rs_*`` reasoning), and the SDK replays those items as
# the next turn's input — ``manager_agent`` even replays them explicitly via
# ``result.to_input_list()``. The APIM route here is a LOAD BALANCER that round-robins
# three independent Azure OpenAI resources, and any resource that did not mint an id
# rejects it:
#
#     400 - "The requested item was created under a different Azure OpenAI resource."
#
# So every turn after the first had a 2-in-3 chance of failing and being retried against
# the next backend. ``store=false`` carries the whole conversation in the request, mints
# no ids, and lets any backend serve any turn — what a round-robin pool requires.
# (Measured on the sibling trAIde bot against this same pool: 59% of all backend calls
# were wasted before the switch, 0% after.)
STATELESS_RUN_CONFIG = RunConfig(model_settings=ModelSettings(store=False))

# --- sampling parameters, and the models that refuse them ---------------------------- #
# Reasoning-family models (o1/o3/o4, gpt-5.x) REJECT `temperature` outright —
# 400 "Unsupported parameter: 'temperature' is not supported with this model" —
# rather than ignoring it. They do their own sampling internally, so there is
# nothing to pass. Every agent therefore builds its ModelSettings through
# `agent_model_settings` below instead of constructing ModelSettings directly,
# so switching AZURE_OPENAI_MODEL cannot break a run in the one place someone
# forgot to update.
#
# On Azure the model name is the DEPLOYMENT name, which the operator chooses, so
# this can only be a good guess: a gpt-5 deployment named "main" is invisible to
# it. `LLM_SUPPORTS_TEMPERATURE=0|1` overrides the guess in both directions.
_NO_SAMPLING = (
    re.compile(r"(?:^|[^a-z0-9])o[1-9](?:[^0-9]|$)"),   # o1, o3-mini, o4 — not gpt-4o
    re.compile(r"gpt-[5-9]"),                            # gpt-5, gpt-5.6-luna, and later
)


def supports_sampling(model_name: str) -> bool:
    """Whether ``temperature``/``top_p`` may be sent to this model."""
    override = settings.llm.supports_temperature
    if override is not None:
        return override
    name = (model_name or "").strip().lower()
    return not any(pattern.search(name) for pattern in _NO_SAMPLING)


def agent_model_settings(*, temperature: float | None = None, **kwargs) -> ModelSettings:
    """``ModelSettings`` with the sampling knobs dropped when the model refuses them.

    Build every agent's settings through this. Passing ``temperature`` to a
    reasoning model is a hard 400, not a warning, so an agent that sets it
    directly stops working the moment the deployment is repointed.
    """
    if temperature is not None:
        if supports_sampling(settings.llm.model):
            kwargs["temperature"] = temperature
        else:
            logger.debug("Model %s does not accept temperature; omitting it.",
                         settings.llm.model)
    return ModelSettings(**kwargs)


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


def _build_azure_client(llm: LLMSettings | None = None) -> AsyncAzureOpenAI:
    llm = llm or settings.llm
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


def _build_apim_client(llm: LLMSettings | None = None) -> AsyncAzureOpenAI:
    """APIM route → Azure-shaped client (see module docstring for why).

    ``azure_endpoint`` is the APIM route *without* ``/openai``; the SDK appends
    it, producing ``{APIM_BASE_URL}/openai/responses?api-version=…``.
    """
    llm = llm or settings.llm
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


def _build_client(llm: LLMSettings) -> AsyncAzureOpenAI:
    """Build a dedicated client for one ``LLMSettings`` — no global mutation."""
    provider = llm.provider
    if provider == "apim":
        return _build_apim_client(llm)
    if provider == "azure":
        return _build_azure_client(llm)
    raise RuntimeError(f"Unknown LLM_PROVIDER={provider!r} (expected 'azure' or 'apim').")


def _log_endpoint(llm: LLMSettings) -> None:
    # Log where calls actually go — the first thing to check when a gateway
    # 404s or times out. (client.base_url carries a /deployments/<model> suffix
    # that the SDK drops for /responses, so it would read as misleading here.)
    endpoint = (llm.apim_base_url if llm.provider == "apim"
                else llm.azure_endpoint).rstrip("/").removesuffix("/openai")
    logger.info("LLM client ready (provider=%s, model=%s, responses → %s/openai/responses)",
                llm.provider, llm.model, endpoint)


def _fingerprint(llm: LLMSettings) -> str:
    """A stable key for one connection that never stores the raw secret.

    Two instructions on the same endpoint+key share one client rather than
    leaking a connection per run; the secret is hashed, never used as a key.
    """
    secret = (llm.apim_subscription_key if llm.provider == "apim" else llm.azure_api_key) or ""
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
    return "|".join([
        llm.provider, llm.model,
        llm.apim_base_url if llm.provider == "apim" else llm.azure_endpoint,
        llm.apim_api_version if llm.provider == "apim" else llm.azure_api_version,
        llm.apim_key_header, digest,
    ])


_MODEL_CACHE: dict[str, OpenAIResponsesModel] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def build_model_for(llm: LLMSettings) -> OpenAIResponsesModel:
    """Return a Responses model bound to a SPECIFIC connection.

    Unlike :func:`build_model`, this does **not** call
    ``set_default_openai_client`` — concurrent scheduler runs each carry their
    own client on the returned model, so a per-instruction override cannot
    clobber another run's default client. Clients are reused across runs by a
    fingerprint of the connection (secret hashed, never stored raw).
    """
    key = _fingerprint(llm)
    with _MODEL_CACHE_LOCK:
        model = _MODEL_CACHE.get(key)
        if model is None:
            client = _build_client(llm)
            _log_endpoint(llm)
            model = OpenAIResponsesModel(model=llm.model, openai_client=client)
            _MODEL_CACHE[key] = model
        return model


@lru_cache(maxsize=1)
def _client():
    client = _build_client(settings.llm)
    set_default_openai_client(client)
    _log_endpoint(settings.llm)
    return client


@lru_cache(maxsize=1)
def build_model() -> OpenAIResponsesModel:
    """Return the shared Responses model used by every agent (env default).

    This still registers the client as the SDK default so the hosted
    ``WebSearchTool`` and any SDK-default consumers route through it.
    """
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
