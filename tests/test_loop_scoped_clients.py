"""API clients must not outlive the event loop that opened their sockets.

The bug these pin, measured live on ``pocvm``: the first run after a restart worked
and every scheduled run after it died in about a second with ::

    RuntimeError: Event loop is closed

thrown out of the very first ``client.responses.create``. Each run is its own
``asyncio.run``, so the process-wide client cache handed run N+1 a connection pool
full of sockets belonging to run N's *closed* loop; evicting one of them calls
``loop.call_soon`` on a loop that is gone. ``httpx2``/``httpcore2`` (openai 3.x)
propagate that instead of swallowing it, which is why a cache that had been correct
for months began failing runs the night the dependency floors went up.
"""
import asyncio

import pytest

from aismm import async_clients
from aismm.async_clients import ClientCache, close_loop_clients


@pytest.fixture(autouse=True)
def _clean_caches():
    async_clients.reset_caches()
    yield
    async_clients.reset_caches()


class _FakeClient:
    """Stands in for an httpx-backed API client, and remembers its loop."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    async def close(self) -> None:
        # The real failure: an asyncio transport's close() does loop.call_soon(),
        # which raises once the loop it belongs to has been closed.
        if self.loop.is_closed():
            raise RuntimeError("Event loop is closed")
        self.closed = True


def test_a_second_run_never_reuses_the_first_runs_client():
    cache = ClientCache("test")
    built = []

    def build():
        client = _FakeClient()
        built.append(client)
        return client

    async def one_run():
        client = cache.get("same-fingerprint", build)
        assert client.loop is asyncio.get_running_loop()
        return client

    first = asyncio.run(one_run())
    second = asyncio.run(one_run())

    assert first is not second, (
        "a client cached across asyncio.run() boundaries carries sockets bound to "
        "a closed loop — that is 'Event loop is closed' on the next run's first call")
    assert len(built) == 2


def test_one_run_shares_one_client():
    """The pooling is still the point — only the cross-run sharing was wrong."""
    cache = ClientCache("test")
    built = []

    async def one_run():
        def build():
            client = _FakeClient()
            built.append(client)
            return client

        a = cache.get("fp", build)
        b = cache.get("fp", build)
        c = cache.get("other-fp", build)
        return a, b, c

    a, b, c = asyncio.run(one_run())
    assert a is b
    assert c is not a
    assert len(built) == 2  # one per fingerprint, not one per call


def test_close_loop_clients_closes_this_loops_clients():
    cache = ClientCache("test")
    seen = {}

    async def one_run():
        client = cache.get("fp", _FakeClient)
        seen["client"] = client
        await close_loop_clients()
        assert not cache._entries, "a closed client must not stay in the cache"

    asyncio.run(one_run())
    assert seen["client"].closed


def test_a_dead_loops_client_is_dropped_and_never_closed():
    """Closing it is exactly what raises, so the safety net must not try."""
    cache = ClientCache("test")

    async def one_run():
        return cache.get("fp", _FakeClient)

    stale = asyncio.run(one_run())          # loop closed, client never closed
    assert cache._entries                   # still cached: nothing tore it down

    async def next_run():
        return cache.get("fp", _FakeClient)

    fresh = asyncio.run(next_run())         # must not raise
    assert fresh is not stale
    assert not stale.closed, "a client on a dead loop must be dropped, not closed"


def test_close_loop_clients_survives_a_client_that_refuses_to_close():
    cache = ClientCache("test")

    class _Stubborn(_FakeClient):
        async def close(self):
            raise RuntimeError("nope")

    async def one_run():
        cache.get("fp", _Stubborn)
        await close_loop_clients()          # best effort; never fatal
        assert not cache._entries

    asyncio.run(one_run())


def test_orchestrator_closes_clients_at_the_loop_boundary():
    """``_run_async`` owns the loop, so it is what must guarantee the teardown."""
    from aismm import orchestrator

    cache = ClientCache("test")
    seen = {}

    async def work():
        seen["client"] = cache.get("fp", _FakeClient)
        return "done"

    assert orchestrator._run_async(work()) == "done"
    assert seen["client"].closed
    assert not cache._entries


def test_the_teardown_still_runs_when_the_run_fails():
    from aismm import orchestrator

    cache = ClientCache("test")
    seen = {}

    async def work():
        seen["client"] = cache.get("fp", _FakeClient)
        raise ValueError("run blew up")

    with pytest.raises(ValueError):
        orchestrator._run_async(work())
    assert seen["client"].closed


def test_the_llm_cache_is_loop_scoped():
    """The real module, not a stand-in: llm.build_model_for must not cross loops."""
    import dataclasses

    from aismm import llm as llm_module
    from aismm.config import LLMSettings

    llm = LLMSettings(provider="azure", model="gpt-5", azure_api_key="k",
                      azure_endpoint="https://e.openai.azure.com",
                      azure_api_version="2025-04-01-preview")

    async def one_run():
        return llm_module.build_model_for(dataclasses.replace(llm))

    first = asyncio.run(one_run())
    second = asyncio.run(one_run())
    assert first._client is not second._client
