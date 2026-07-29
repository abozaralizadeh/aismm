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
    for check in (_check_store_backends, _check_imports, _check_config):
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
