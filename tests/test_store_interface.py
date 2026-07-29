"""Every Store backend must implement the whole interface.

A deploy of commit 611e6d9 crash-looped because ``base.py`` declared
``get_run``/``count_runs`` while ``azure_store.py`` had not implemented them yet:
Python only raises ``TypeError: Can't instantiate abstract class`` when something
first calls ``get_store()``, which under gunicorn is at worker boot. These tests
turn that into a red test instead of an outage.
"""
import inspect

import pytest

from aismm.store.azure_store import AzureStore
from aismm.store.base import Store
from aismm.store.local_store import LocalStore

BACKENDS = [LocalStore, AzureStore]


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda c: c.__name__)
def test_backend_is_concrete(backend):
    missing = sorted(getattr(backend, "__abstractmethods__", ()) or ())
    assert not missing, (
        f"{backend.__name__} is missing {missing}. Adding a method to Store means "
        f"implementing it in BOTH backends, or the app dies at first use."
    )


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda c: c.__name__)
def test_backend_signatures_accept_the_interface_arguments(backend):
    """A backend that silently drops a keyword would fail only at runtime."""
    for name, declared in inspect.getmembers(Store, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        implementation = getattr(backend, name, None)
        assert implementation is not None, f"{backend.__name__}.{name} is missing"

        declared_kwargs = {
            p.name for p in inspect.signature(declared).parameters.values()
            if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
        }
        impl_params = inspect.signature(implementation).parameters
        if any(p.kind is p.VAR_KEYWORD for p in impl_params.values()):
            continue                       # **kwargs accepts everything
        accepted = {p.name for p in impl_params.values()}
        missing = declared_kwargs - accepted
        assert not missing, (
            f"{backend.__name__}.{name} does not accept {sorted(missing)} "
            f"declared by Store.{name}"
        )


def test_the_preflight_script_agrees():
    """The deploy gate and the test suite must not disagree about what's broken."""
    from scripts import preflight

    preflight.problems.clear()
    preflight.notes.clear()
    preflight._check_store_backends()
    assert preflight.problems == []


def test_preflight_detects_a_missing_method(monkeypatch):
    """Prove the gate actually fires — the 611e6d9 failure, reproduced."""
    from scripts import preflight

    class Incomplete(LocalStore):
        pass

    # Pretend a method was never implemented.
    monkeypatch.setattr(Incomplete, "__abstractmethods__", frozenset({"count_runs", "get_run"}))
    monkeypatch.setattr(preflight, "_check_store_backends",
                        lambda: preflight.problems.append(
                            f"Incomplete does not implement "
                            f"{sorted(Incomplete.__abstractmethods__)}"))
    preflight.problems.clear()
    preflight._check_store_backends()
    assert preflight.problems and "count_runs" in preflight.problems[0]
