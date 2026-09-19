"""Sora pool: index alignment, job-level round-robin, and failover.

No network: the client's ``create_clip`` is monkeypatched, so these exercise the
load-balancing logic only.
"""
import asyncio
import time
import types

import httpx
import pytest

from aismm.config import SoraSettings
from aismm.tools import sora_client, sora_config


def _with_max_attempts(monkeypatch, value, *, health_ttl_seconds=900):
    """Settings is a frozen singleton, so swap the name the module reads.

    The inner object is a REAL ``SoraSettings``, not a namespace with the one
    field in use: a hand-rolled stand-in silently loses every field added later,
    and `health_ttl_seconds` broke nine tests that way.
    """
    monkeypatch.setattr(sora_config, "settings",
                        types.SimpleNamespace(sora=SoraSettings(
                            max_attempts=value, health_ttl_seconds=health_ttl_seconds)))


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


def test_every_resource_is_named_in_the_failure_not_just_the_last(pool3, monkeypatch):
    """Three days of failed video runs were reported as ONE 404.

    The old message kept the last exception, so a pool whose members fail for
    DIFFERENT reasons (a rotated key on one, no Sora deployment on the other)
    read as a single problem on a single resource — and the resource named was
    whichever answered last.
    """
    request = httpx.Request("POST", "https://r0.openai.azure.com/openai/v1/videos")
    bodies = {
        "https://r0.openai.azure.com": (401, "invalid subscription key"),
        "https://r1.openai.azure.com": (404, "The API deployment for this resource does not exist"),
        "https://r2.openai.azure.com": (429, "too many requests"),
    }

    async def per_resource(resource, prompt, seconds, size, ref=None):
        status, text = bodies[resource["endpoint"]]
        raise httpx.HTTPStatusError(
            str(status), request=request,
            response=httpx.Response(status, request=request, text=text))

    monkeypatch.setattr(sora_client, "create_clip", per_resource)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(sora_client.create_clip_with_failover("p", 8, "720x1280"))

    message = str(exc.value)
    for host in ("r0.openai.azure.com", "r1.openai.azure.com", "r2.openai.azure.com"):
        assert host in message
    assert "api-key is rejected" in message          # the 401, explained
    assert "no 'sora-2' deployment" in message       # the 404, explained
    assert "too many requests" in message            # the 429, verbatim
    # One resource may recover, so this must NOT claim the whole pool is broken.
    assert "may be temporary" in message


def test_a_pool_that_is_entirely_misconfigured_says_so(pool3, monkeypatch):
    """Retrying is hopeless here, and the operator has to be told that."""
    request = httpx.Request("POST", "https://r0.openai.azure.com/openai/v1/videos")
    response = httpx.Response(401, request=request, text="invalid subscription key")

    async def always_401(resource, prompt, seconds, size, ref=None):
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(sora_client, "create_clip", always_401)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(sora_client.create_clip_with_failover("p", 8, "720x1280"))

    assert "Every resource in the pool is misconfigured" in str(exc.value)
    assert "smoke_sora.py --check" in str(exc.value)


def test_a_resource_already_known_unusable_is_not_asked_again(pool3, monkeypatch):
    """One shot proves a resource dead; the rest of the sequence must not re-pay for it."""
    request = httpx.Request("POST", "https://r0.openai.azure.com/openai/v1/videos")
    response = httpx.Response(401, request=request, text="invalid subscription key")
    seen = []

    async def dead_r0(resource, prompt, seconds, size, ref=None):
        seen.append(resource["endpoint"])
        if resource["endpoint"] == "https://r0.openai.azure.com":
            raise httpx.HTTPStatusError("401", request=request, response=response)
        return b"MP4", "job-1"

    monkeypatch.setattr(sora_client, "create_clip", dead_r0)
    known: dict[str, str] = {}
    for _ in range(3):
        asyncio.run(sora_client.create_clip_with_failover("p", 8, "720x1280", unusable=known))

    assert list(known) == ["https://r0.openai.azure.com"]
    assert seen.count("https://r0.openai.azure.com") == 1


def test_a_skipped_resource_is_still_named_when_everything_fails(pool3, monkeypatch):
    """Skipping must never hide a resource: it is the whole diagnosis."""
    request = httpx.Request("POST", "https://r0.openai.azure.com/openai/v1/videos")
    response = httpx.Response(404, request=request,
                              text="The API deployment for this resource does not exist")

    async def always_404(resource, prompt, seconds, size, ref=None):
        raise httpx.HTTPStatusError("404", request=request, response=response)

    monkeypatch.setattr(sora_client, "create_clip", always_404)
    known = {"https://r0.openai.azure.com": "its api-key is rejected (measured earlier)"}
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(sora_client.create_clip_with_failover("p", 8, "720x1280", unusable=known))

    assert "r0.openai.azure.com" in str(exc.value)      # never asked, still reported
    assert "api-key is rejected" in str(exc.value)


@pytest.mark.parametrize("status, body, expected", [
    (401, "invalid subscription key", "api-key is rejected"),
    (403, "forbidden", "api-key is rejected"),
    (404, "The API deployment for this resource does not exist", "no 'sora-2' deployment"),
    (404, "not found", "does not serve the Sora videos API"),
    (429, "rate limited", ""),          # busy, not broken — retrying elsewhere may work
    (500, "server error", ""),
    (None, "", ""),                     # a timeout: nothing was answered at all
])
def test_permanent_failures_are_told_apart_from_passing_ones(status, body, expected):
    reason = sora_client._permanent_reason(status, body, {"model": "sora-2"})
    assert (expected in reason) if expected else (reason == "")


# --- the free health check ----------------------------------------------------- #

def _checking(monkeypatch, status, payload):
    """Answer the deployments listing with ``payload`` (text or JSON)."""
    def handler(request):
        assert "api-version=2022-12-01" in str(request.url), "the newer surfaces 404 here"
        if isinstance(payload, str):
            return httpx.Response(status, request=request, text=payload)
        return httpx.Response(status, request=request, json=payload)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: original(**{**kw, "transport": transport}))


def test_a_resource_with_the_model_deployed_is_healthy(monkeypatch):
    _checking(monkeypatch, 200, {"data": [{"id": "gpt-4.1"}, {"id": "sora-2"}]})
    result = asyncio.run(sora_client.check_resource(
        {"endpoint": "https://r0.openai.azure.com", "key": "k", "model": "sora-2"}))
    assert result["ok"] is True


def test_a_resource_without_the_model_names_what_it_does_have(monkeypatch):
    """The pool carried a member that had never had Sora on it; only a create ever said so."""
    _checking(monkeypatch, 200, {"data": [{"id": "gpt-4.1"}, {"id": "model-router"}]})
    result = asyncio.run(sora_client.check_resource(
        {"endpoint": "https://r1.openai.azure.com", "key": "k", "model": "sora-2"}))
    assert result["ok"] is False
    assert "no 'sora-2' deployment" in result["detail"]
    assert "gpt-4.1" in result["detail"] and "model-router" in result["detail"]


def test_a_rotated_key_is_reported_as_the_key(monkeypatch):
    _checking(monkeypatch, 401, "invalid subscription key")
    result = asyncio.run(sora_client.check_resource(
        {"endpoint": "https://r0.openai.azure.com", "key": "stale", "model": "sora-2"}))
    assert result["ok"] is False and "api-key is rejected" in result["detail"]


def test_a_check_never_raises(monkeypatch):
    """It is a diagnostic; an unreachable host is an answer, not a crash."""
    def boom(request):
        raise httpx.ConnectError("no route to host", request=request)

    _transport = httpx.MockTransport(boom)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: original(**{**kw, "transport": _transport}))
    result = asyncio.run(sora_client.check_resource(
        {"endpoint": "https://gone.openai.azure.com", "key": "k", "model": "sora-2"}))
    assert result["ok"] is False and "unreachable" in result["detail"]


def test_format_http_error_includes_status_url_and_body():
    request = httpx.Request("POST", "https://r0.openai.azure.com/openai/v1/videos")
    response = httpx.Response(429, request=request, text="rate limited")
    msg = sora_client.format_http_error(
        httpx.HTTPStatusError("429", request=request, response=response))
    assert "429" in msg and "openai/v1/videos" in msg and "rate limited" in msg


# --- the pool REMEMBERS which resources are dead ------------------------------- #
# The whole point of a pool is to route around a resource that is down — an
# expired subscription, a rotated key, a model never deployed. That only works if
# the knowledge outlives the clip that bought it. It used to die with the caller
# (a per-sequence `unusable` dict), so every run started blind, the round-robin
# handed out the same dead resource first, and every run paid the same 401 before
# reaching the one that works. Observed on run 5afab777: veronica 401, then gioak
# — on a pool where veronica had been dead for days.

def test_a_dead_resource_is_skipped_by_later_picks(pool3):
    sora_config.mark_unusable(pool3[0], "its api-key is rejected")
    picked = {sora_config.next_resource()["endpoint"] for _ in range(6)}
    assert pool3[0]["endpoint"] not in picked
    assert picked == {pool3[1]["endpoint"], pool3[2]["endpoint"]}


def test_load_is_still_spread_across_the_ones_that_work(pool3):
    """Skipping the dead member must not collapse onto a single survivor."""
    sora_config.mark_unusable(pool3[0], "no deployment")
    picked = [sora_config.next_resource()["endpoint"] for _ in range(6)]
    assert picked.count(pool3[1]["endpoint"]) == 3
    assert picked.count(pool3[2]["endpoint"]) == 3


def test_the_verdict_expires_so_a_fixed_pool_heals_itself(monkeypatch, pool3):
    """A rotated key and a missing deployment are both fixed in Azure, where this
    process cannot see it — so a permanent-forever blacklist would need a restart
    to undo."""
    _with_max_attempts(monkeypatch, 0, health_ttl_seconds=60)
    sora_config.mark_unusable(pool3[0], "its api-key is rejected")
    assert sora_config.unusable_reason(pool3[0])

    # Capture the real clock FIRST — patching `time.time` and then calling it
    # inside the replacement is unbounded recursion.
    later = time.time() + 61
    monkeypatch.setattr(sora_config.time, "time", lambda: later)
    assert sora_config.unusable_reason(pool3[0]) == ""
    assert pool3[0]["endpoint"] in {sora_config.next_resource()["endpoint"] for _ in range(6)}


def test_a_new_key_for_the_same_endpoint_starts_clean(pool3):
    """The fix for a rejected key is a DIFFERENT key on that same endpoint. Keying
    the verdict on the endpoint alone would leave the fixed resource skipped."""
    sora_config.mark_unusable(pool3[0], "its api-key is rejected")
    rotated = {**pool3[0], "key": "rotated-key"}
    assert sora_config.unusable_reason(rotated) == ""


def test_success_clears_an_earlier_verdict(pool3):
    sora_config.mark_unusable(pool3[0], "its api-key is rejected")
    sora_config.mark_healthy(pool3[0])
    assert sora_config.unusable_reason(pool3[0]) == ""


def test_a_pool_where_everything_is_marked_bad_still_tries(pool3):
    """Stepping aside must never become "nothing to try" — a stale verdict has to
    be able to be wrong."""
    for r in pool3:
        sora_config.mark_unusable(r, "no deployment")
    assert sora_config.next_resource()["endpoint"] in {r["endpoint"] for r in pool3}


def test_the_memory_can_be_switched_off(monkeypatch, pool3):
    _with_max_attempts(monkeypatch, 0, health_ttl_seconds=0)
    sora_config.mark_unusable(pool3[0], "its api-key is rejected")
    assert sora_config.unusable_reason(pool3[0]) == ""


def test_known_unusable_reports_what_is_being_skipped(pool3):
    sora_config.mark_unusable(pool3[1], "no 'sora-2' deployment")
    assert sora_config.known_unusable() == {pool3[1]["endpoint"]: "no 'sora-2' deployment"}


# --- and the failover both FEEDS and READS that memory -------------------------- #

def _failing(monkeypatch, status_by_endpoint, *, ok_endpoint=None):
    """create_clip that fails per endpoint, and records who was asked."""
    asked = []

    async def fake_create(resource, prompt, seconds, size, ref=None):
        asked.append(resource["endpoint"])
        if resource["endpoint"] == ok_endpoint:
            return b"mp4", "job-1"
        status = status_by_endpoint.get(resource["endpoint"], 500)
        request = httpx.Request("POST", f"{resource['endpoint']}/openai/v1/videos")
        response = httpx.Response(status, request=request,
                                  text='{"error":{"message":"denied"}}')
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr(sora_client, "create_clip", fake_create)
    return asked


def test_a_dead_resource_is_not_asked_again_on_the_NEXT_call(monkeypatch, pool3):
    """The across-runs claim: one clip pays for the discovery, later ones don't."""
    dead, alive = pool3[0]["endpoint"], pool3[1]["endpoint"]
    asked = _failing(monkeypatch, {dead: 401}, ok_endpoint=alive)

    asyncio.run(sora_client.create_clip_with_failover("p", 4, "720x1280"))
    first_round = list(asked)
    asked.clear()

    # A separate call with its OWN empty `unusable` dict — i.e. the next run.
    asyncio.run(sora_client.create_clip_with_failover("p", 4, "720x1280"))
    assert dead in first_round, "the first call should have discovered it"
    assert dead not in asked, "the second call paid for the same discovery again"


def test_a_transient_failure_is_not_remembered(monkeypatch, pool3):
    """429/5xx say nothing about whether the resource can serve Sora."""
    flaky, alive = pool3[0]["endpoint"], pool3[1]["endpoint"]
    _failing(monkeypatch, {flaky: 503}, ok_endpoint=alive)
    asyncio.run(sora_client.create_clip_with_failover("p", 4, "720x1280"))
    assert sora_config.unusable_reason(pool3[0]) == ""


def test_a_pool_of_stale_verdicts_recovers_on_the_first_success(monkeypatch, pool3):
    """Every member marked dead, but the pool has since been fixed. The fallback
    tries one anyway, it works, and that verdict is cleared — so the pool climbs
    back out without a restart."""
    async def always_ok(resource, prompt, seconds, size, ref=None):
        return b"mp4", "job-1"

    monkeypatch.setattr(sora_client, "create_clip", always_ok)
    for r in pool3:
        sora_config.mark_unusable(r, "stale verdict")

    _mp4, _job, served = asyncio.run(
        sora_client.create_clip_with_failover("p", 4, "720x1280"))
    assert sora_config.unusable_reason(served) == ""
    assert served["endpoint"] not in sora_config.known_unusable()


def test_the_skip_is_logged_when_it_changes_not_once_per_clip(monkeypatch, pool3, caplog):
    """An eight-shot sequence repeating the same paragraph eight times is the
    noise that hides the line that matters."""
    dead, alive = pool3[0]["endpoint"], pool3[1]["endpoint"]
    _failing(monkeypatch, {dead: 401}, ok_endpoint=alive)

    asyncio.run(sora_client.create_clip_with_failover("p", 4, "720x1280"))   # discovers
    with caplog.at_level("INFO", logger="aismm.sora"):
        for _ in range(5):
            asyncio.run(sora_client.create_clip_with_failover("p", 4, "720x1280"))
    said = [r for r in caplog.records if "routing around" in r.getMessage()]
    assert len(said) == 1, f"logged {len(said)} times for five clips"
    assert dead.split("://")[-1] in said[0].getMessage()


def test_recovery_is_logged_too(monkeypatch, pool3, caplog):
    sora_config.mark_unusable(pool3[0], "its api-key is rejected")
    sora_client._reported_skips.update({pool3[0]["endpoint"]})
    _failing(monkeypatch, {}, ok_endpoint=pool3[1]["endpoint"])
    sora_config.reset_health()
    with caplog.at_level("INFO", logger="aismm.sora"):
        asyncio.run(sora_client.create_clip_with_failover("p", 4, "720x1280"))
    assert any("healthy again" in r.getMessage() for r in caplog.records)
