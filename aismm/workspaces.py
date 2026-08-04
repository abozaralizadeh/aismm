"""Workspaces: who can see which accounts, instructions and runs.

A workspace is a **silo** — its own connected social accounts, instructions,
runs and staged posts. Partitioning only the instructions would have been less
work and would not have given anyone a private workspace: every member could
still have published to every connected account, which is the thing that
actually matters when several people share one deployment.

There is only ONE kind of workspace. Every one starts private to whoever created
it and becomes shared the moment they add a member, so *any* workspace can be
shared and none is permanently locked to one person. A separate "personal" kind
that could also be shared was a distinction without a difference.

**Signing in for the first time gets you your own workspace and nothing else.**
A new colleague lands in an empty space and writes their own instructions,
rather than opening the dashboard onto someone else's live accounts. They see
another workspace only when its owner invites them.

Identity comes from SSO; there is no local user table, because there are no
local passwords to keep. A membership is keyed by the email the provider
returns, lowercased.

**With SSO disabled the dashboard is already unauthenticated**, so inventing a
user there would be security theatre. In that mode one implicit local operator
is an owner of everything: workspaces still exist and can be switched between,
nothing is hidden, and local development is unchanged.

Content written before workspaces existed carries no ``workspace_id``. Exactly
one workspace is marked ``claims_unassigned`` and owns those rows **at read
time** (the store's workspace filter takes several ids, so the dashboard asks
for ``[that_id, ""]``). The first operator to sign in inherits it, because they
are the person whose content it is.
"""
from __future__ import annotations

import logging

from .models import Workspace, WorkspaceMember, WorkspaceRole

logger = logging.getLogger("aismm.workspaces")

# The implicit operator when SSO is off. Not a real address, and never emailed.
LOCAL_USER = "local@aismm"
LOCAL_USER_NAME = "Local operator"

LEGACY_WORKSPACE_NAME = "Shared"
# Older builds created the migration workspace with this as its creator and no
# owner at all, which left it unmanageable — nobody could invite anyone to it.
_SYSTEM_CREATOR = "system"


def normalize(email: str) -> str:
    return (email or "").strip().lower()


def default_name(email: str, display_name: str = "") -> str:
    who = (display_name or "").strip() or normalize(email).split("@")[0] or "My"
    return f"{who}'s workspace"


# --- the workspace that owns pre-existing content --------------------------- #

def has_unassigned(store) -> bool:
    """Is there content from before workspaces existed?

    Only accounts and instructions are checked: both are small, and a run or a
    staged post cannot exist without one of them.
    """
    return (any(not a.workspace_id for a in store.list_accounts())
            or any(not i.workspace_id for i in store.list_instructions()))


def legacy_workspace(store):
    """The workspace that owns rows carrying no ``workspace_id``, if any."""
    for workspace in store.list_workspaces():
        if workspace.claims_unassigned:
            return workspace
    return None


def _claim(store, workspace, email: str, display_name: str = ""):
    """Make ``workspace`` this operator's own: theirs, named for them, owned.

    Used both for the workspace an upgrade inherits and to repair one an older
    build created ownerless — its membership could never be changed by anyone,
    so it could never be shared or tidied up.
    """
    if workspace.created_by in ("", _SYSTEM_CREATOR):
        workspace.created_by = email
        if workspace.name in ("", LEGACY_WORKSPACE_NAME, "Default"):
            workspace.name = default_name(email, display_name)
    workspace = store.upsert_workspace(workspace)
    if not owners(store, workspace.id):
        store.add_member(WorkspaceMember(workspace_id=workspace.id, email=email,
                                         role=WorkspaceRole.owner,
                                         display_name=display_name))
        logger.info("%s now owns %s", email, workspace.name)
    return workspace


def _adopt_legacy_for(store, email: str, display_name: str = ""):
    """Make the first operator's own workspace the one holding existing content.

    Nothing else can claim it: an allowlist may be a whole domain, so "the
    people who were already using this" is not knowable — but the first person
    through the door after an upgrade is the person whose accounts these are.

    It is renamed to *their* workspace rather than left as a separate "Default"
    they have to go and find. Landing on an empty dashboard when your instructions
    are one click away in another workspace reads exactly like data loss.
    """
    workspace = legacy_workspace(store)
    if workspace is None:
        # An older build's migration workspace, created ownerless.
        workspace = next((w for w in store.list_workspaces()
                          if w.created_by == _SYSTEM_CREATOR), None)
        if workspace is None and not has_unassigned(store):
            return None
        if workspace is None:
            workspace = Workspace(name=default_name(email, display_name))
        workspace.claims_unassigned = True

    workspace = _claim(store, workspace, email, display_name)
    logger.info("%s owns %s, which holds everything created before workspaces existed",
                email, workspace.name)
    return workspace


def adopt_orphans(store, workspace_id: str) -> int:
    """Write a workspace onto unassigned rows. Housekeeping, not a repair —
    they are already visible in the workspace that claims them."""
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
        logger.info("Assigned %d previously unassigned row(s) to workspace %s",
                    moved, workspace_id)
    return moved


# --- sign-in ---------------------------------------------------------------- #

def ensure_user(store, email: str, display_name: str = "") -> list[Workspace]:
    """Everything a just-signed-in identity should have. Returns their workspaces.

    Their own workspace, and nothing else — a new colleague does not land in
    someone else's accounts. The exception is the very first operator, who
    inherits the content that predates workspaces because it is theirs.
    """
    email = normalize(email)
    if not email:
        return []
    mine = accessible(store, email)

    # An older build added everyone to a migration workspace it created with NO
    # owner, so its membership could never be changed. Whoever signs in claims
    # it — which also renames it to their own workspace, so the content they
    # already had does not sit in a separate space they have to go and find.
    for workspace in mine:
        if not owners(store, workspace.id):
            if workspace.created_by == _SYSTEM_CREATOR:
                workspace.claims_unassigned = True
            _claim(store, workspace, email, display_name)
    mine = accessible(store, email)

    if not mine and not _all_members(store):
        # The first operator through the door after an upgrade: the content that
        # predates workspaces is theirs, and nothing else could claim it.
        _adopt_legacy_for(store, email, display_name)
        mine = accessible(store, email)

    # Everyone ends up with a workspace of their OWN. Checking ownership rather
    # than membership matters: someone invited to a shared workspace before they
    # ever signed in would otherwise land straight into it and never get a space
    # to start their own work in.
    if not any(w.created_by == email for w in mine):
        create(store, default_name(email, display_name), email, display_name=display_name)
        logger.info("Created a workspace for %s", email)
        mine = accessible(store, email)
    return mine


def _all_members(store) -> list[WorkspaceMember]:
    members = []
    for workspace in store.list_workspaces():
        members.extend(store.list_members(workspace.id))
    return members


def make_owner(store, workspace, email: str, display_name: str = "") -> WorkspaceMember:
    """Promote (or add) ``email`` as an owner of ``workspace``.

    Idempotent, and never removes anyone: an existing owner stays one. This is
    the repair for a workspace left ownerless — with no owner its membership can
    never be changed, so nobody can invite anyone or hand it over.
    """
    member = store.add_member(WorkspaceMember(
        workspace_id=workspace.id, email=normalize(email), role=WorkspaceRole.owner,
        display_name=display_name))
    if workspace.created_by in ("", _SYSTEM_CREATOR):
        workspace.created_by = normalize(email)
        store.upsert_workspace(workspace)
    logger.info("%s is now an owner of %s", normalize(email), workspace.name)
    return member


def rename(store, workspace, name: str) -> Workspace:
    workspace.name = (name or "").strip() or workspace.name
    return store.upsert_workspace(workspace)


def find(store, needle: str):
    """A workspace by id or by (case-insensitive) name — for the CLI."""
    needle = (needle or "").strip()
    if not needle:
        return None
    rows = store.list_workspaces()
    return (next((w for w in rows if w.id == needle), None)
            or next((w for w in rows if w.name.lower() == needle.lower()), None))


# --- access ---------------------------------------------------------------- #

def accessible(store, email: str | None, *, unauthenticated: bool = False) -> list[Workspace]:
    """The workspaces this identity may use.

    ``unauthenticated`` is the SSO-off case: one operator, everything visible.
    """
    if unauthenticated:
        rows = store.list_workspaces()
        if rows:
            return rows
        return [store.upsert_workspace(Workspace(
            name=default_name(LOCAL_USER, LOCAL_USER_NAME), created_by=LOCAL_USER,
            claims_unassigned=True))]
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


def is_shared(store, workspace_id: str) -> bool:
    """More than one person can see it. Any workspace may become shared."""
    return len(store.list_members(workspace_id)) > 1


def create(store, name: str, email: str, *, display_name: str = "") -> Workspace:
    """Create a workspace with its creator as the first owner.

    It is private until they add someone — there is no separate "personal" kind
    to choose, and no workspace that cannot later be shared.
    """
    email = normalize(email) or LOCAL_USER
    workspace = store.upsert_workspace(Workspace(
        name=(name or "").strip() or "Untitled workspace", created_by=email))
    store.add_member(WorkspaceMember(workspace_id=workspace.id, email=email,
                                     role=WorkspaceRole.owner, display_name=display_name))
    return workspace


def landing(store, email: str | None, *, unauthenticated: bool = False):
    """Which workspace to open when there is no remembered choice.

    Their OWN — the one they own and nobody invited them to. Someone who is a
    guest in a busy shared workspace should still land in their own space rather
    than in the middle of somebody else's schedule.
    """
    mine = accessible(store, email, unauthenticated=unauthenticated)
    if not mine:
        return None
    key = normalize(email)
    owned = [w for w in mine if w.created_by == key]
    return (owned or mine)[0]


def scope_for(workspace) -> str | list[str]:
    """The store filter for everything this workspace can see.

    The workspace marked ``claims_unassigned`` ALSO owns rows that carry no
    workspace — content written before workspaces existed. Every read about a
    workspace has to use this, not the bare id: listing through the scope while
    counting through the id is what made a workspace show a full page of
    instructions above "0 instruction(s)".
    """
    if workspace is None:
        return "\x00none"                                 # matches nothing
    return [workspace.id, ""] if workspace.claims_unassigned else workspace.id


def content_counts(store, workspace) -> dict:
    """What this workspace holds — shown on the page and before Delete.

    Takes the workspace itself rather than an id, so the scope rule above is
    applied and cannot be forgotten at the call site.
    """
    scope = scope_for(workspace)
    return {
        "accounts": len(store.list_accounts(workspace_id=scope)),
        "instructions": len(store.list_instructions(workspace_id=scope)),
        "runs": store.count_runs(workspace_id=scope),
    }
