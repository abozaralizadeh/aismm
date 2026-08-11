"""Who may SEE and USE a given LLM connection.

The app has no per-row ACL anywhere else (workspaces scope whole silos, not
individual rows), so this is the net-new primitive for the sharing model the
owner asked for:

* **own** — the creator always sees full detail and may edit/delete/share.
* **workspace-shared** — any creator may share their own connection with the
  members of its workspace; they see the **name only**.
* **people-shared** — ONLY the site owner may share a connection with specific
  people (across workspaces); they too see the **name only**.
* **the ``.env`` deployment model** (``provider == "env"``) — usable by the
  owner, and by anyone the owner people-shares it with. Nobody else can reshare
  it, because only the owner may set ``shared_with`` on their own rows.

``is_owned`` is what a template checks to decide full-detail vs name-only, and
``can_select`` is the gate the instruction picker and the run resolver share so
a stale/forbidden ``llm_config_id`` can never smuggle a private connection into
another user's run.
"""
from __future__ import annotations

from collections.abc import Iterable

from .config import settings
from .models import (
    ENV_IMAGE_ID, ENV_LLM_ID, ENV_VIDEO_ID, LLMConfig, ProviderConfig,
)

_ENV_PROVIDER_IDS = {"image": ENV_IMAGE_ID, "video": ENV_VIDEO_ID}


def env_config() -> LLMConfig:
    """The deployment ``.env`` sentinel, synthesised when its row is absent.

    The row is created lazily (when the owner first opens Settings), but a run
    or a picker may need it before then. It is owned by the first configured
    site owner, so ``can_select`` grants it to the owner and to anyone the owner
    later people-shares it with — and nobody else.
    """
    owner = (settings.auth.owner_emails[0].strip().lower()
             if settings.auth.owner_emails else "")
    return LLMConfig(id=ENV_LLM_ID, provider="env", name="Deployment default",
                     created_by=owner)


def is_owned(config: LLMConfig, email: str) -> bool:
    """Does this identity OWN the connection (full detail, edit/delete/share)?"""
    addr = (email or "").strip().lower()
    return bool(addr) and config.created_by.strip().lower() == addr


def can_select(
    config: LLMConfig,
    email: str,
    member_workspace_ids: Iterable[str],
    *,
    is_owner: bool,
) -> bool:
    """May this identity pick this connection for an instruction / run?"""
    if not config.enabled:
        return False
    addr = (email or "").strip().lower()
    if config.is_env:
        # Deployment default: the owner always, plus anyone people-shared it.
        return bool(is_owner) or (bool(addr) and addr in config.shared_with)
    if is_owned(config, addr):
        return True
    if config.shared_with_workspace and config.workspace_id in set(member_workspace_ids):
        return True
    return bool(addr) and addr in config.shared_with


def visible_configs(
    store,
    email: str,
    member_workspace_ids: Iterable[str],
    *,
    is_owner: bool,
) -> list[LLMConfig]:
    """Every connection this identity may select, own first then shared.

    Used for the Settings list and the instruction picker. Ordering is: own
    connections (by name), then shared ones (by name) — so a user's own configs
    lead and the deployment default sits among whatever they may use.
    """
    member_ids = set(member_workspace_ids)
    selectable = [
        c for c in store.list_llm_configs()
        if can_select(c, email, member_ids, is_owner=is_owner)
    ]
    selectable.sort(key=lambda c: (not is_owned(c, email), c.label.lower()))
    return selectable


# --- image / video (ProviderConfig) share the same ACL --------------------- #
# is_owned/can_select are duck-typed (they only touch the sharing fields), so
# ProviderConfig rows pass through them unchanged. Only the env sentinel and the
# list source differ per kind.

def env_provider_config(kind: str) -> ProviderConfig:
    """The deployment ``.env`` sentinel for an image/video ``kind``, synthesised
    when its row is absent (created lazily when the owner first opens Settings).
    Owner-owned, so ``can_select`` grants it to the owner and to anyone the owner
    people-shares it with — and nobody else."""
    owner = (settings.auth.owner_emails[0].strip().lower()
             if settings.auth.owner_emails else "")
    return ProviderConfig(id=_ENV_PROVIDER_IDS.get(kind, ""), kind=kind,
                          name="Deployment default", created_by=owner)


def visible_provider_configs(
    store,
    kind: str,
    email: str,
    member_workspace_ids: Iterable[str],
    *,
    is_owner: bool,
) -> list[ProviderConfig]:
    """Every image/video connection of ``kind`` this identity may select, own
    first then shared — the Settings list and the instruction picker."""
    member_ids = set(member_workspace_ids)
    selectable = [
        c for c in store.list_provider_configs(kind=kind)
        if can_select(c, email, member_ids, is_owner=is_owner)
    ]
    selectable.sort(key=lambda c: (not is_owned(c, email), c.label.lower()))
    return selectable
