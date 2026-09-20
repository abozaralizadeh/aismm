"""Async API clients belong to an EVENT LOOP, not just to a connection.

Every run goes through ``orchestrator._run_async`` → ``asyncio.run()``, which builds
a fresh event loop and **closes it** when the run returns. An httpx connection pool
keeps idle keep-alive sockets whose asyncio transports belong to the loop that opened
them, so a client cached process-wide and handed to the NEXT run eventually evicts one
of those connections — and closing an asyncio transport does ``loop.call_soon(...)``
on the loop that created it::

    RuntimeError: Event loop is closed

raised straight out of the first ``client.responses.create``, failing the run in under
a second with nothing in it to blame. Measured live on ``pocvm``: the first run after
a restart always worked, every scheduled run after it died in 0.4–1.4s (runs b7dcfcb8,
fe070394, e5878e37, 6da06258). ``httpx2``/``httpcore2``, which **openai 3.x** brought
in, propagate that out of ``handle_async_request``; the older httpx did not — which is
why a cache that had been correct for months started failing runs the night the
dependency floors went up.

So a cache key is ``(event loop, connection fingerprint)`` and the loop's clients are
closed by :func:`close_loop_clients` before the loop ends — the same discipline
``browse_tool.close_browser`` uses for Chromium, for the same reason.

A client whose loop is already gone is **dropped, never closed**: closing it is
precisely what raises. Its sockets are reclaimed by the garbage collector, which closes
the file descriptors directly (a ``ResourceWarning`` at worst, never an exception), so
the safety net cannot itself become the bug.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from typing import Callable, TypeVar

logger = logging.getLogger("aismm.async_clients")

T = TypeVar("T")

_CACHES: list["ClientCache"] = []
_CACHES_LOCK = threading.Lock()


def _running_loop():
    """The loop this call belongs to, or ``None`` when called from sync code.

    A ``None`` key means "not bound to any loop" — a one-shot script that builds a
    client before ``asyncio.run``. Nothing in the service does that; every model is
    resolved inside ``run_for_account``.
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


async def _aclose(client: object) -> None:
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


class ClientCache:
    """Async clients keyed by ``(running event loop, fingerprint)``.

    The fingerprint is the caller's: two instructions on the same endpoint and key
    share one client *within a run*, which is what the pooling is for. What they no
    longer share is a client across runs.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._entries: dict[tuple[int, str], tuple[object, object]] = {}
        self._lock = threading.Lock()
        with _CACHES_LOCK:
            _CACHES.append(self)

    def get(self, key: str, build: Callable[[], T]) -> T:
        loop = _running_loop()
        cache_key = (id(loop), key)
        with self._lock:
            self._forget_dead_loops()
            entry = self._entries.get(cache_key)
            # ``is not loop`` is load-bearing: CPython reuses the address of a freed
            # loop, so id() alone would eventually hand a new run the dead client of
            # an old one — exactly the failure this cache exists to prevent.
            if entry is None or entry[0] is not loop:
                entry = (loop, build())
                self._entries[cache_key] = entry
            return entry[1]  # type: ignore[return-value]

    def _forget_dead_loops(self) -> None:
        """Safety net for a loop that ended without :func:`close_loop_clients`."""
        for cache_key, (loop, _client) in list(self._entries.items()):
            if loop is not None and loop.is_closed():
                self._entries.pop(cache_key, None)

    async def aclose(self) -> None:
        """Close and forget the clients belonging to the running loop."""
        loop = _running_loop()
        if loop is None:
            return
        with self._lock:
            doomed = [(k, e[1]) for k, e in self._entries.items() if e[0] is loop]
            for cache_key, _client in doomed:
                self._entries.pop(cache_key, None)
        for _cache_key, client in doomed:
            try:
                await _aclose(client)
            except Exception as exc:  # noqa: BLE001 - teardown is best effort
                logger.debug("%s: closing a client failed: %s", self._name, exc)

    def clear(self) -> None:
        """Forget every entry without closing anything (tests)."""
        with self._lock:
            self._entries.clear()


async def close_loop_clients() -> None:
    """Close every cached client that belongs to the running loop.

    Call this at the end of the coroutine handed to ``asyncio.run``, so the loop never
    closes with live sockets still pinned to it.
    """
    with _CACHES_LOCK:
        caches = list(_CACHES)
    for cache in caches:
        await cache.aclose()


def reset_caches() -> None:
    """Forget everything in every cache without closing (tests)."""
    with _CACHES_LOCK:
        caches = list(_CACHES)
    for cache in caches:
        cache.clear()
