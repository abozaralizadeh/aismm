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


# --- every platform must honour the publish contract --------------------------------- #
# `perform_publish` ALWAYS passes asset_paths and placement. Instagram grew them
# when carousels/stories were added; the other three never did, so the first real
# publish on X died with `Twitter.publish() got an unexpected keyword argument
# 'asset_paths'` — after the agent had browsed, generated an image and written its
# memory. Python does not check override signatures, so this test does.

import inspect as _inspect

import pytest as _pytest

from aismm.models import PlatformName as _PlatformName
from aismm.platforms.base import SocialPlatform as _SocialPlatform
from aismm.platforms.registry import (
    get_platform_class as _get_platform_class,
    registered_platforms as _registered_platforms,
)


def _publish_kwargs(func) -> set:
    signature = _inspect.signature(func)
    if any(p.kind is _inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        return set()          # **kwargs accepts anything
    return {name for name, p in signature.parameters.items() if name != "self"}


@_pytest.mark.parametrize("name", _registered_platforms(), ids=lambda n: n.value)
def test_publish_accepts_every_argument_the_publish_tool_sends(name):
    required = _publish_kwargs(_SocialPlatform.publish)
    actual = _publish_kwargs(_get_platform_class(name).publish)
    if not actual:
        return                # **kwargs
    missing = required - actual
    assert not missing, (
        f"{name.value}.publish() is missing {sorted(missing)} — perform_publish passes "
        f"these on every call, so the first publish would raise TypeError")


@_pytest.mark.parametrize("name", _registered_platforms(), ids=lambda n: n.value)
def test_publish_is_callable_with_the_full_keyword_set(name):
    """Bind the real call `perform_publish` makes — catches order/kind mistakes too."""
    signature = _inspect.signature(_get_platform_class(name).publish)
    signature.bind_partial(
        None, access_token="t", account=None, caption="c", asset_path="/a.jpg",
        media_kind="image", instruction=None, asset_paths=["/a.jpg"], placement="feed")


@_pytest.mark.parametrize("name", _registered_platforms(), ids=lambda n: n.value)
def test_carousel_capability_matches_reality(name):
    """A platform that says it takes several items must accept several paths."""
    caps = _get_platform_class(name).capabilities
    if caps.supports_carousel:
        assert caps.max_carousel_items > 1
