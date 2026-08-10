"""Fail a deploy BEFORE it takes the service down.

``setup_service.sh`` runs this after installing dependencies and before
restarting the unit. If it exits non-zero the deploy aborts and the *currently
running* service is left alone — far better than gunicorn crash-looping on a
worker that can't boot.

It catches the failures that only appear at import/instantiation time, which
tests catch too but a hurried commit can skip:

* a ``Store`` backend missing a method that ``base.py`` declares abstract —
  Python raises ``TypeError: Can't instantiate abstract class`` the first time
  anything calls ``get_store()``, which under gunicorn is a boot loop;
* an import error anywhere in the app, tool registry, or platform integrations;
* obviously broken configuration (no LLM provider credentials at all).

Run it by hand any time:  python scripts/preflight.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

problems: list[str] = []
notes: list[str] = []


def _check_store_backends() -> None:
    """Every Store implementation must be concrete — this is the boot-loop bug."""
    from aismm.store.azure_store import AzureStore
    from aismm.store.base import Store
    from aismm.store.local_store import LocalStore

    for cls in (LocalStore, AzureStore):
        missing = sorted(getattr(cls, "__abstractmethods__", ()) or ())
        if missing:
            problems.append(
                f"{cls.__name__} does not implement {len(missing)} method(s) declared in "
                f"Store: {', '.join(missing)}. Add them to aismm/store/"
                f"{cls.__name__.replace('Store', '').lower()}_store.py."
            )
    declared = {name for name in vars(Store) if not name.startswith("_")}
    notes.append(f"Store interface: {len(declared)} methods, both backends concrete"
                 if not problems else "Store interface: MISMATCH")


def _check_publish_signatures() -> None:
    """Every platform must accept what perform_publish always sends.

    Python does not check override signatures, so a platform missing
    ``asset_paths``/``placement`` looks fine until the first real publish, which
    then dies with TypeError *after* the agent has spent a run producing media.
    """
    import inspect

    from aismm.platforms.base import SocialPlatform
    from aismm.platforms.registry import get_platform_class, registered_platforms

    def names(func):
        signature = inspect.signature(func)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in signature.parameters.values()):
            return set()
        return {n for n in signature.parameters if n != "self"}

    required = names(SocialPlatform.publish)
    for platform in registered_platforms():
        cls = get_platform_class(platform)
        actual = names(cls.publish)
        missing = (required - actual) if actual else set()
        if missing:
            problems.append(
                f"{cls.__name__}.publish() is missing {', '.join(sorted(missing))} — "
                f"perform_publish passes these on every call, so publishing to "
                f"{platform.value} would raise TypeError."
            )
    if not any("publish() is missing" in p for p in problems):
        notes.append(f"Publish contract: {len(registered_platforms())} platform(s) OK")


def _check_reply_signatures() -> None:
    """Every platform that declares ``supports_comments`` must accept what
    ``engagement.perform_reply`` always sends to ``reply_to_target``.

    Same failure mode as the publish contract: an override with a narrower
    signature looks fine until the first live reply, which then dies with
    TypeError after the agent has already read and composed a reply.
    """
    import inspect

    from aismm.platforms.base import SocialPlatform
    from aismm.platforms.registry import get_platform_class, registered_platforms

    def names(func):
        signature = inspect.signature(func)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in signature.parameters.values()):
            return set()
        return {n for n in signature.parameters if n != "self"}

    required = names(SocialPlatform.reply_to_target)
    checked = 0
    for platform in registered_platforms():
        cls = get_platform_class(platform)
        if not getattr(cls.capabilities, "supports_comments", False):
            continue
        checked += 1
        actual = names(cls.reply_to_target)
        missing = (required - actual) if actual else set()
        if missing:
            problems.append(
                f"{cls.__name__}.reply_to_target() is missing {', '.join(sorted(missing))} — "
                f"perform_reply passes these on every call, so replying on "
                f"{platform.value} would raise TypeError."
            )
    if not any("reply_to_target() is missing" in p for p in problems):
        notes.append(f"Reply contract: {checked} comment-capable platform(s) OK")


def _check_like_signatures() -> None:
    """Every platform that declares ``supports_liking`` must accept what the like
    tool sends to ``like_target`` — the same drift guard as the reply contract."""
    import inspect

    from aismm.platforms.base import SocialPlatform
    from aismm.platforms.registry import get_platform_class, registered_platforms

    def names(func):
        signature = inspect.signature(func)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in signature.parameters.values()):
            return set()
        return {n for n in signature.parameters if n != "self"}

    required = names(SocialPlatform.like_target)
    checked = 0
    for platform in registered_platforms():
        cls = get_platform_class(platform)
        if not getattr(cls.capabilities, "supports_liking", False):
            continue
        checked += 1
        actual = names(cls.like_target)
        missing = (required - actual) if actual else set()
        if missing:
            problems.append(
                f"{cls.__name__}.like_target() is missing {', '.join(sorted(missing))} — "
                f"the like tool passes these on every call, so liking on "
                f"{platform.value} would raise TypeError."
            )
    if not any("like_target() is missing" in p for p in problems):
        notes.append(f"Like contract: {checked} like-capable platform(s) OK")


def _check_metrics_signatures() -> None:
    """Every platform that declares ``supports_metrics`` must accept what
    ``orchestrator.refresh_metrics`` sends to ``fetch_post_metrics`` — the same
    drift guard as the publish / reply / like contracts."""
    import inspect

    from aismm.platforms.base import SocialPlatform
    from aismm.platforms.registry import get_platform_class, registered_platforms

    def names(func):
        signature = inspect.signature(func)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in signature.parameters.values()):
            return set()
        return {n for n in signature.parameters if n != "self"}

    required = names(SocialPlatform.fetch_post_metrics)
    checked = 0
    for platform in registered_platforms():
        cls = get_platform_class(platform)
        if not getattr(cls.capabilities, "supports_metrics", False):
            continue
        checked += 1
        actual = names(cls.fetch_post_metrics)
        missing = (required - actual) if actual else set()
        if missing:
            problems.append(
                f"{cls.__name__}.fetch_post_metrics() is missing {', '.join(sorted(missing))} — "
                f"refresh_metrics passes these on every call, so polling "
                f"{platform.value} would raise TypeError."
            )
    if not any("fetch_post_metrics() is missing" in p for p in problems):
        notes.append(f"Metrics contract: {checked} metrics-capable platform(s) OK")


def _check_search_signatures() -> None:
    """Every platform that declares ``supports_search`` must accept what the
    outreach search tools send to ``search_content`` — the same drift guard as the
    publish / reply / like / metrics contracts."""
    import inspect

    from aismm.platforms.base import SocialPlatform
    from aismm.platforms.registry import get_platform_class, registered_platforms

    def names(func):
        signature = inspect.signature(func)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in signature.parameters.values()):
            return set()
        return {n for n in signature.parameters if n != "self"}

    required = names(SocialPlatform.search_content)
    checked = 0
    for platform in registered_platforms():
        cls = get_platform_class(platform)
        if not getattr(cls.capabilities, "supports_search", False):
            continue
        checked += 1
        actual = names(cls.search_content)
        missing = (required - actual) if actual else set()
        if missing:
            problems.append(
                f"{cls.__name__}.search_content() is missing {', '.join(sorted(missing))} — "
                f"the outreach search tool passes these on every call, so searching "
                f"{platform.value} would raise TypeError."
            )
    if not any("search_content() is missing" in p for p in problems):
        notes.append(f"Search contract: {checked} search-capable platform(s) OK")


def _check_dm_signatures() -> None:
    """Every platform that declares ``supports_dms`` must accept what the DM read
    tool sends to ``list_dms`` — the same drift guard as the other contracts. The
    DM *reply* uses ``reply_to_target`` (with ``reply_to``), which the reply
    contract already covers because every DM platform is also comment-capable."""
    import inspect

    from aismm.platforms.base import SocialPlatform
    from aismm.platforms.registry import get_platform_class, registered_platforms

    def names(func):
        signature = inspect.signature(func)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in signature.parameters.values()):
            return set()
        return {n for n in signature.parameters if n != "self"}

    required = names(SocialPlatform.list_dms)
    checked = 0
    for platform in registered_platforms():
        cls = get_platform_class(platform)
        if not getattr(cls.capabilities, "supports_dms", False):
            continue
        checked += 1
        actual = names(cls.list_dms)
        missing = (required - actual) if actual else set()
        if missing:
            problems.append(
                f"{cls.__name__}.list_dms() is missing {', '.join(sorted(missing))} — "
                f"the DM read tool passes these on every call, so reading DMs on "
                f"{platform.value} would raise TypeError."
            )
    if not any("list_dms() is missing" in p for p in problems):
        notes.append(f"DM contract: {checked} DM-capable platform(s) OK")


def _check_imports() -> None:
    """Import everything a worker imports at boot, minus the side effects."""
    import aismm.dashboard.app  # noqa: F401  - route + template wiring
    import aismm.platforms  # noqa: F401      - platform registrations
    import aismm.tools  # noqa: F401          - tool registry

    from aismm.platforms.registry import registered_platforms
    from aismm.tools.registry import registered_tool_names

    notes.append(f"Platforms registered: {len(registered_platforms())}")
    notes.append(f"Tools registered: {len(registered_tool_names())}")


def _check_config() -> None:
    from aismm.config import settings

    llm = settings.llm
    if llm.provider == "apim" and not (llm.apim_base_url and llm.apim_subscription_key):
        problems.append("LLM_PROVIDER=apim but APIM_BASE_URL / APIM_SUBSCRIPTION_KEY are unset.")
    elif llm.provider == "azure" and not (llm.azure_api_key and llm.azure_endpoint):
        problems.append("LLM_PROVIDER=azure but AZURE_OPENAI_API_KEY / _ENDPOINT are unset.")
    notes.append(f"Storage backend: {'azure' if settings.use_azure_store else 'local'}")
    notes.append(f"Dashboard sign-in: {'on' if settings.auth.enabled else 'OFF'}")


def main() -> int:
    for check in (_check_store_backends, _check_imports, _check_publish_signatures,
                  _check_reply_signatures, _check_like_signatures,
                  _check_metrics_signatures, _check_search_signatures,
                  _check_dm_signatures, _check_config):
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - any failure here blocks the deploy
            problems.append(f"{check.__name__.lstrip('_')}: {type(exc).__name__}: {exc}")

    for note in notes:
        print(f"  {note}")
    if problems:
        print("\nPREFLIGHT FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  ✗ {problem}", file=sys.stderr)
        return 1
    print("  preflight OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
