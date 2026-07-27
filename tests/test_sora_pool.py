"""Sora pool: index alignment, job-level round-robin, and failover.

No network: the client's ``create_clip`` is monkeypatched, so these exercise the
load-balancing logic only.
"""
import asyncio
import types

import httpx
import pytest

from aismm.config import SoraSettings
from aismm.tools import sora_client, sora_config


def _with_max_attempts(monkeypatch, value):
    """Settings is a frozen singleton, so swap the name the module reads."""
    monkeypatch.setattr(sora_config, "settings",
                        types.SimpleNamespace(sora=types.SimpleNamespace(max_attempts=value)))


@pytest.fixture()
def pool3(monkeypatch):
    """A three-resource pool with the round-robin cursor reset.

    Also pins ``max_attempts`` to auto so the tests don't depend on a developer's
    real ``.env`` (settings are read at import time).
    """
    resources = [
        {"endpoint": f"https://r{i}.openai.azure.com", "key": f"key{i}", "model": "sora-2"}
        for i in range(3)
    ]
    monkeypatch.setattr(sora_config, "pool", lambda: list(resources))
    monkeypatch.setattr(sora_config, "_rr_index", 0, raising=False)
    _with_max_attempts(monkeypatch, 0)
    return resources


# --- config: endpoints/keys/models align by index ---------------------------- #

def test_pool_aligns_keys_and_models_by_index():
    s = SoraSettings(endpoints=["https://a", "https://b"], keys=["ka", "kb"],
                     models=["sora-2", "sora-2-pro"])
    assert s.pool() == [
        {"endpoint": "https://a", "key": "ka", "model": "sora-2"},
        {"endpoint": "https://b", "key": "kb", "model": "sora-2-pro"},
    ]


def test_single_key_and_model_apply_to_every_endpoint():
    s = SoraSettings(endpoints=["https://a", "https://b"], keys=["shared"], models=["sora-2"])
    assert [r["key"] for r in s.pool()] == ["shared", "shared"]
    assert [r["model"] for r in s.pool()] == ["sora-2", "sora-2"]


def test_pool_disabled_without_a_key():
    assert SoraSettings(endpoints=["https://a"], keys=[]).enabled is False


# --- round-robin -------------------------------------------------------------- #

def test_next_resource_cycles_through_the_pool(pool3):
    endpoints = [r["endpoint"] for r in pool3]
    picked = [sora_config.next_resource()["endpoint"] for _ in range(7)]
    assert picked == (endpoints * 3)[:7]


def test_next_resource_skips_excluded_endpoints(pool3):
    excluded = {pool3[0]["endpoint"], pool3[1]["endpoint"]}
    for _ in range(5):
        assert sora_config.next_resource(exclude_endpoints=excluded)["endpoint"] \
            == pool3[2]["endpoint"]


def test_exclusion_is_ignored_when_it_would_empty_the_pool(pool3):
    all_endpoints = {r["endpoint"] for r in pool3}
    assert sora_config.next_resource(exclude_endpoints=all_endpoints)["endpoint"] in all_endpoints


def test_next_resource_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr(sora_config, "pool", list)
    with pytest.raises(RuntimeError, match="No Sora resources configured"):
        sora_config.next_resource()


def test_max_attempts_auto_walks_the_pool_capped_at_three(monkeypatch, pool3):
    _with_max_attempts(monkeypatch, 0)
    assert sora_config.max_attempts() == 3          # 3-resource pool
    monkeypatch.setattr(sora_config, "pool", lambda: pool3[:2])
    assert sora_config.max_attempts() == 2          # never more than the pool
    monkeypatch.setattr(sora_config, "pool", list)
    assert sora_config.max_attempts() == 1          # empty pool still attempts once


def test_max_attempts_env_override_is_clamped_to_the_pool(monkeypatch, pool3):
    _with_max_attempts(monkeypatch, 5)
    assert sora_config.max_attempts() == 3
    _with_max_attempts(monkeypatch, 1)
    assert sora_config.max_attempts() == 1


# --- failover ----------------------------------------------------------------- #

def test_failover_moves_to_a_different_resource(pool3, monkeypatch):
    """A failing resource must not consume every attempt (the 401-out-of-credits case)."""
    seen = []

    async def fake_create_clip(resource, prompt, seconds, size, ref=None):
        seen.append(resource["endpoint"])
        if len(seen) < 3:
            raise httpx.ConnectError("boom")
        return b"MP4", "job-123"

    monkeypatch.setattr(sora_client, "create_clip", fake_create_clip)
    mp4, job_id, resource = asyncio.run(
        sora_client.create_clip_with_failover("p", 8, "720x1280"))

    assert (mp4, job_id) == (b"MP4", "job-123")
    assert resource["endpoint"] == seen[-1]      # the resource that actually served it
    assert len(set(seen)) == 3                   # each attempt on a DIFFERENT resource


def test_failover_gives_up_with_the_azure_error_body(pool3, monkeypatch):
    request = httpx.Request("POST", "https://r0.openai.azure.com/openai/v1/videos")
    response = httpx.Response(401, request=request, text='{"error":"out of credits"}')

    async def always_401(resource, prompt, seconds, size, ref=None):
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(sora_client, "create_clip", always_401)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(sora_client.create_clip_with_failover("p", 8, "720x1280"))

    # The Azure body is what says *why* — httpx's own message stops at the status.
    assert "out of credits" in str(exc.value)
    assert "3 resource(s)" in str(exc.value)


def test_format_http_error_includes_status_url_and_body():
    request = httpx.Request("POST", "https://r0.openai.azure.com/openai/v1/videos")
    response = httpx.Response(429, request=request, text="rate limited")
    msg = sora_client.format_http_error(
        httpx.HTTPStatusError("429", request=request, response=response))
    assert "429" in msg and "openai/v1/videos" in msg and "rate limited" in msg
