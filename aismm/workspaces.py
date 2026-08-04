"""Workspaces: who can see which accounts, instructions and runs.

A workspace is a **silo** — its own connected social accounts, instructions,
runs and staged posts. Partitioning only the instructions would have been less
work and would not have given anyone a private workspace: every member could
still have published to every connected account, which is the thing that
actually matters when several people share one deployment.

Identity comes from SSO; there is no local user table, because there are no
local passwords to keep. A membership is keyed by the email the provider
returns, lowercased.

**With SSO disabled the dashboard is already unauthenticated**, so inventing a
user there would be security theatre. In that mode one implicit local operator
is an owner of everything: workspaces still exist and can be switched between,
nothing is hidden, and local development is unchanged.

Upgrading an existing deployment must not hide anyone's work: content written
before workspaces existed carries no ``workspace_id``, and the default shared
workspace claims those rows at READ time (the store's workspace filter takes
several ids, so the dashboard asks for ``[default_id, ""]``). Everyone who signs
in joins that workspace, so the upgrade changes nothing anyone can see.
"""
from __future__ import annotations

import logging

from .models import Workspace, WorkspaceKind, WorkspaceMember, WorkspaceRole

logger = logging.getLogger("aismm.workspaces")

# The implicit operator when SSO is off. Not a real address, and never emailed.
LOCAL_USER = "local@aismm"
LOCAL_USER_NAME = "Local operator"

DEFAULT_WORKSPACE_NAME = "Default"


def normalize(email: str) -> str:
    return (email or "").strip().lower()


# --- bootstrap / migration ------------------------------------------------- #

def ensure_default(store, *, adopt: bool = False) -> Workspace:
    """The shared workspace that existing content belongs to. Idempotent.

    Content written before workspaces existed carries no ``workspace_id``, and
    the DEFAULT workspace claims those rows **at read time** (the store's
    workspace filter accepts several ids, so the dashboard asks for
    ``[default_id, ""]``). Resolving it on read rather than rewriting every row
    on boot means the upgrade cannot lose anything by having its migration
    skipped, interrupted, or run against a half-written table — and it costs no
    scan on a runs table that grows without bound.

    ``adopt=True`` does rewrite them, which is only worth doing to tidy up.
    """
    existing = [w for w in store.list_workspaces() if w.auto_join]
    if existing:
        workspace = existing[0]
    else:
        rest = store.list_workspaces()
        workspace = rest[0] if rest else store.upsert_workspace(Workspace(
            name=DEFAULT_WORKSPACE_NAME, kind=WorkspaceKind.shared, auto_join=True,
            created_by="system"))
        if not workspace.auto_join:
            workspace.auto_join = True
            store.upsert_workspace(workspace)
    if adopt:
        adopt_orphans(store, workspace.id)
    return workspace


def adopt_orphans(store, workspace_id: str) -> int:
    """Give every unassigned row a home. Returns how many were moved."""
    moved = 0
    for account in store.list_accounts():
        if not account.workspace_id:
            account.workspace_id = workspace_id
            store.upsert_account(account)
            moved += 1
    for instruction in store.list_instructions():
        if not instruction.workspace_id:
            instruction.workspace_id = workspace_id
            store.upsert_instruction(instruction)
            moved += 1
    for run in store.list_runs(limit=10_000):
        if not run.workspace_id:
            run.workspace_id = workspace_id
            store.update_run(run)
            moved += 1
    for staged in store.list_staged(limit=10_000):
        if not staged.workspace_id:
            staged.workspace_id = workspace_id
            store.update_staged(staged)
            moved += 1
    if moved:
        logger.info("Adopted %d pre-existing rows into workspace %s", moved, workspace_id)
    return moved


def ensure_user(store, email: str, display_name: str = "") -> list[Workspace]:
    """Everything a just-signed-in identity should have. Returns their workspaces.

    Joins the auto-join workspace (an allowlist can be a *domain*, so the people
    who may sign in cannot be enumerated in advance — the first sign-in is the
    only moment we learn who they are) and creates their personal one.
    """
    email = normalize(email)
    if not email:
        return []
    ensure_default(store)
    memberships = {m.workspace_id for m in store.list_memberships(email)}
    for workspace in store.list_workspaces():
        if workspace.auto_join and workspace.id not in memberships:
            store.add_member(WorkspaceMember(workspace_id=workspace.id, email=email,
                                             role=WorkspaceRole.member,
                                             display_name=display_name))
            logger.info("Added %s to the shared workspace %s", email, workspace.name)
    if not any(w.kind is WorkspaceKind.personal for w in accessible(store, email)):
        personal = store.upsert_workspace(Workspace(
            name=f"{display_name or email.split('@')[0]}'s workspace",
            kind=WorkspaceKind.personal, created_by=email))
        store.add_member(WorkspaceMember(workspace_id=personal.id, email=email,
                                         role=WorkspaceRole.owner,
                                         display_name=display_name))
        logger.info("Created a personal workspace for %s", email)
    return accessible(store, email)


# --- access ---------------------------------------------------------------- #

def accessible(store, email: str | None, *, unauthenticated: bool = False) -> list[Workspace]:
    """The workspaces this identity may use.

    ``unauthenticated`` is the SSO-off case: one operator, everything visible.
    """
    if unauthenticated:
        workspaces = store.list_workspaces()
        return workspaces or [ensure_default(store)]
    email = normalize(email)
    if not email:
        return []
    ids = {m.workspace_id for m in store.list_memberships(email)}
    return [w for w in store.list_workspaces() if w.id in ids]


def role_in(store, workspace_id: str, email: str | None,
            *, unauthenticated: bool = False) -> WorkspaceRole | None:
    """This identity's role, or ``None`` when they are not a member."""
    if unauthenticated:
        return WorkspaceRole.owner
    email = normalize(email)
    if not (email and workspace_id):
        return None
    for member in store.list_memberships(email):
        if member.workspace_id == workspace_id:
            return member.role
    return None


def can_view(store, workspace_id: str, email: str | None,
             *, unauthenticated: bool = False) -> bool:
    return role_in(store, workspace_id, email, unauthenticated=unauthenticated) is not None


def can_admin(store, workspace_id: str, email: str | None,
              *, unauthenticated: bool = False) -> bool:
    """Manage membership, connect accounts, rename or delete the workspace."""
    return role_in(store, workspace_id, email,
                   unauthenticated=unauthenticated) is WorkspaceRole.owner


def owners(store, workspace_id: str) -> list[WorkspaceMember]:
    return [m for m in store.list_members(workspace_id) if m.role is WorkspaceRole.owner]


def create(store, name: str, email: str, *, display_name: str = "",
           kind: WorkspaceKind = WorkspaceKind.shared) -> Workspace:
    """Create a workspace with its creator as the first owner."""
    workspace = store.upsert_workspace(Workspace(
        name=(name or "").strip() or "Untitled workspace", kind=kind,
        created_by=normalize(email) or LOCAL_USER))
    store.add_member(WorkspaceMember(workspace_id=workspace.id,
                                     email=normalize(email) or LOCAL_USER,
                                     role=WorkspaceRole.owner,
                                     display_name=display_name))
    return workspace


def content_counts(store, workspace_id: str) -> dict:
    """What deleting this workspace would orphan — shown before the button."""
    return {
        "accounts": len(store.list_accounts(workspace_id=workspace_id)),
        "instructions": len(store.list_instructions(workspace_id=workspace_id)),
        "runs": store.count_runs(workspace_id=workspace_id),
    }
