"""Workspaces: several people on one deployment, sharing only what they choose.

A workspace is a silo — its own accounts, instructions, runs and staged posts.
Partitioning only the instructions would have been less work and would not have
produced a private workspace at all: every member could still have published to
every connected account.

There is one kind of workspace: private to its creator until they add a member,
and shareable in every case. Signing in for the first time gets you your OWN
workspace and nothing else — a new colleague lands in an empty space rather
than in the middle of someone else's live accounts.

Identity comes from SSO. With SSO off the dashboard is already unauthenticated,
so one implicit local operator owns everything rather than a fictional user
being invented to guard nothing.
"""
import dataclasses

import pytest

from aismm import config as config_module
from aismm import workspaces
from aismm.config import AuthSettings
from aismm.dashboard import app as app_module
from aismm.dashboard import sso
from aismm.models import (
    Account, Instruction, PlatformName, Run, RunStatus, StagedPost, Workspace,
    WorkspaceMember,
)
from aismm.store.local_store import LocalStore

ISSUER = "https://id.example.com"
CLIENT_ID = "client-abc"
DISCOVERY = {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
             "token_endpoint": f"{ISSUER}/token"}


# --- the store layer ------------------------------------------------------------------ #

def test_content_is_listed_per_workspace(store):
    a = workspaces.create(store, "Alpha", "a@x.com")
    b = workspaces.create(store, "Beta", "b@x.com")
    store.upsert_account(Account(platform=PlatformName.instagram, handle="alpha",
                                 external_id="1", workspace_id=a.id))
    store.upsert_account(Account(platform=PlatformName.twitter, handle="beta",
                                 external_id="2", workspace_id=b.id))
    assert [x.handle for x in store.list_accounts(workspace_id=a.id)] == ["alpha"]
    assert [x.handle for x in store.list_accounts(workspace_id=b.id)] == ["beta"]
    assert len(store.list_accounts()) == 2          # unscoped still sees everything


def test_instructions_runs_and_staged_are_scoped_too(store):
    a = workspaces.create(store, "Alpha", "a@x.com")
    b = workspaces.create(store, "Beta", "b@x.com")
    for ws, name in ((a, "mine"), (b, "theirs")):
        instr = store.upsert_instruction(Instruction(name=name, workspace_id=ws.id))
        store.add_run(Run(instruction_id=instr.id, account_id="x", workspace_id=ws.id,
                          status=RunStatus.published))
        store.add_staged(StagedPost(instruction_id=instr.id, account_id="x",
                                    workspace_id=ws.id))
    assert [i.name for i in store.list_instructions(workspace_id=a.id)] == ["mine"]
    assert store.count_runs(workspace_id=a.id) == 1
    assert len(store.list_runs(workspace_id=b.id)) == 1
    assert len(store.list_staged(workspace_id=a.id)) == 1


def test_the_claiming_workspace_also_owns_rows_written_before_workspaces_existed(store):
    """The upgrade path. Resolved at READ time, so it cannot be lost by a
    migration that was skipped or interrupted."""
    store.upsert_account(Account(platform=PlatformName.instagram, handle="legacy",
                                 external_id="9"))          # no workspace_id
    workspaces.ensure_user(store, "first@example.com", "First")
    legacy = workspaces.legacy_workspace(store)
    assert legacy is not None
    assert store.list_accounts(workspace_id=legacy.id) == []
    scoped = store.list_accounts(workspace_id=workspaces.scope_for(legacy))
    assert [a.handle for a in scoped] == ["legacy"]


def test_the_first_operator_inherits_and_OWNS_the_pre_existing_content(store):
    """It used to be created ownerless, so nobody could ever share or manage it."""
    store.upsert_instruction(Instruction(name="legacy"))
    workspaces.ensure_user(store, "first@example.com", "First")
    legacy = workspaces.legacy_workspace(store)
    assert [m.email for m in workspaces.owners(store, legacy.id)] == ["first@example.com"]
    assert workspaces.can_admin(store, legacy.id, "first@example.com")


def test_an_ownerless_workspace_from_an_older_build_gets_an_owner(store):
    """Upgrading past that bug must repair it rather than leave it stuck."""
    stranded = store.upsert_workspace(Workspace(name="Default", created_by="system"))
    store.add_member(WorkspaceMember(workspace_id=stranded.id, email="first@example.com"))
    assert workspaces.owners(store, stranded.id) == []
    workspaces.ensure_user(store, "first@example.com", "First")
    assert [m.email for m in workspaces.owners(store, stranded.id)] == ["first@example.com"]
    assert store.get_workspace(stranded.id).claims_unassigned


def test_upgrading_leaves_the_operator_with_ONE_workspace_holding_their_content(store):
    """The shape after an upgrade, end to end.

    An earlier build put everyone in an ownerless "Default" and left each person
    a second, empty workspace — so the operator landed on a blank dashboard with
    their real instructions one click away in a space they had to go and find.
    That reads exactly like data loss.
    """
    stranded = store.upsert_workspace(Workspace(name="Default", created_by="system",
                                                claims_unassigned=True))
    store.add_member(WorkspaceMember(workspace_id=stranded.id, email="abozar@example.com"))
    store.upsert_account(Account(platform=PlatformName.instagram, external_id="1"))
    store.upsert_instruction(Instruction(name="Comicbook"))

    mine = workspaces.ensure_user(store, "abozar@example.com", "Abozar")
    assert [w.name for w in mine] == ["Abozar's workspace"]
    assert workspaces.content_counts(store, mine[0])["instructions"] == 1
    assert workspaces.landing(store, "abozar@example.com").id == mine[0].id
    assert not workspaces.is_shared(store, mine[0].id)


def test_someone_invited_before_their_first_signin_still_gets_their_own(store):
    """Otherwise they land straight into a shared workspace and never have a
    space of their own to start work in."""
    host = workspaces.create(store, "Busy team", "host@example.com")
    store.add_member(WorkspaceMember(workspace_id=host.id, email="guest@example.com"))
    mine = workspaces.ensure_user(store, "guest@example.com", "Guest")
    assert "Guest's workspace" in [w.name for w in mine]
    assert workspaces.landing(store, "guest@example.com").name == "Guest's workspace"


def test_ownership_can_be_taken_by_hand(store):
    """`cli workspaces --owner you@example.com` — the direct repair for a
    workspace left ownerless, without waiting for a sign-in to fix it."""
    stranded = store.upsert_workspace(Workspace(name="Default", created_by="system",
                                                claims_unassigned=True))
    store.add_member(WorkspaceMember(workspace_id=stranded.id, email="me@example.com"))
    workspaces.make_owner(store, stranded, "Me@Example.com", "Me")
    assert workspaces.can_admin(store, stranded.id, "me@example.com")
    assert store.get_workspace(stranded.id).created_by == "me@example.com"
    # Only one membership row: it promoted, it did not add a second.
    assert len(store.list_members(stranded.id)) == 1


def test_taking_ownership_adds_someone_who_was_not_a_member(store):
    workspace = workspaces.create(store, "Theirs", "them@example.com")
    workspaces.make_owner(store, workspace, "me@example.com")
    assert workspaces.can_admin(store, workspace.id, "me@example.com")
    assert workspaces.can_admin(store, workspace.id, "them@example.com")   # unchanged


def test_taking_ownership_is_idempotent(store):
    workspace = workspaces.create(store, "Mine", "me@example.com")
    workspaces.make_owner(store, workspace, "me@example.com")
    workspaces.make_owner(store, workspace, "me@example.com")
    assert len(store.list_members(workspace.id)) == 1


def test_a_workspace_can_be_found_by_id_or_name(store):
    """So the CLI does not force you to copy a uuid."""
    workspace = workspaces.create(store, "Client campaigns", "me@example.com")
    assert workspaces.find(store, workspace.id).id == workspace.id
    assert workspaces.find(store, "client campaigns").id == workspace.id
    assert workspaces.find(store, "nope") is None


def test_a_fresh_deployment_gets_no_legacy_workspace(store):
    """Nothing predates workspaces here, so there is nothing to inherit."""
    workspaces.ensure_user(store, "first@example.com", "First")
    assert [w.name for w in store.list_workspaces()] == ["First's workspace"]


def test_what_the_claiming_workspace_LISTS_is_what_it_COUNTS(store):
    """Reported: it showed a full page of instructions above "0 instruction(s)".
    The listing used the workspace's scope and the counter used the bare id."""
    store.upsert_account(Account(platform=PlatformName.instagram, external_id="9"))
    store.upsert_instruction(Instruction(name="legacy"))
    store.add_run(Run(instruction_id="i", account_id="a", status=RunStatus.published))
    workspaces.ensure_user(store, "first@example.com", "First")
    legacy = workspaces.legacy_workspace(store)

    scope = workspaces.scope_for(legacy)
    counts = workspaces.content_counts(store, legacy)
    assert counts["accounts"] == len(store.list_accounts(workspace_id=scope)) == 1
    assert counts["instructions"] == len(store.list_instructions(workspace_id=scope)) == 1
    assert counts["runs"] == 1


def test_an_ordinary_workspace_does_not_claim_unassigned_rows(store):
    """Only one workspace does — otherwise every workspace would show them."""
    mine = workspaces.create(store, "Mine", "me@example.com")
    store.upsert_instruction(Instruction(name="legacy"))
    assert workspaces.scope_for(mine) == mine.id
    assert workspaces.content_counts(store, mine)["instructions"] == 0


def test_adopting_orphans_is_available_as_a_tidy_up(store):
    store.upsert_account(Account(platform=PlatformName.instagram, external_id="9"))
    store.upsert_instruction(Instruction(name="old"))
    workspaces.ensure_user(store, "first@example.com", "First")
    legacy = workspaces.legacy_workspace(store)
    assert workspaces.adopt_orphans(store, legacy.id) == 2
    assert len(store.list_accounts(workspace_id=legacy.id)) == 1
    assert workspaces.adopt_orphans(store, legacy.id) == 0      # idempotent


# --- signing in --------------------------------------------------------------- #

def test_a_new_signin_gets_one_workspace_of_their_own(store):
    mine = workspaces.ensure_user(store, "Me@Example.com", "Me")
    assert [w.name for w in mine] == ["Me's workspace"]
    assert workspaces.can_admin(store, mine[0].id, "me@example.com")
    # ...and the email is normalized, or a second login makes a second identity.
    assert all(m.email == "me@example.com" for m in store.list_memberships("me@example.com"))


def test_a_LATER_colleague_sees_nothing_of_anyone_elses(store):
    """The point of the redesign: they start empty and write their own."""
    workspaces.ensure_user(store, "first@example.com", "First")
    store.upsert_instruction(Instruction(
        name="Live campaign",
        workspace_id=workspaces.accessible(store, "first@example.com")[0].id))

    theirs = workspaces.ensure_user(store, "new@example.com", "New")
    assert [w.name for w in theirs] == ["New's workspace"]
    assert store.list_instructions(workspace_id=workspaces.scope_for(theirs[0])) == []


def test_signing_in_twice_does_not_duplicate_anything(store):
    workspaces.ensure_user(store, "me@example.com", "Me")
    before = len(store.list_workspaces())
    workspaces.ensure_user(store, "me@example.com", "Me")
    assert len(store.list_workspaces()) == before
    assert len(store.list_memberships("me@example.com")) == before


def test_you_land_in_your_own_workspace_not_one_you_were_invited_to(store):
    """A guest in a busy shared workspace should not open onto someone else's
    schedule."""
    host = workspaces.create(store, "Busy team", "host@example.com")
    workspaces.ensure_user(store, "guest@example.com", "Guest")
    store.add_member(WorkspaceMember(workspace_id=host.id, email="guest@example.com"))
    assert workspaces.landing(store, "guest@example.com").name == "Guest's workspace"


# --- sharing ------------------------------------------------------------------- #

def test_a_new_workspace_is_private_until_someone_is_added(store):
    mine = workspaces.create(store, "Mine", "me@example.com")
    assert not workspaces.is_shared(store, mine.id)
    store.add_member(WorkspaceMember(workspace_id=mine.id, email="them@example.com"))
    assert workspaces.is_shared(store, mine.id)


def test_ANY_workspace_can_be_shared_including_your_first_one(store):
    """There is no kind of workspace that is permanently locked to one person."""
    mine = workspaces.ensure_user(store, "me@example.com", "Me")[0]
    store.add_member(WorkspaceMember(workspace_id=mine.id, email="them@example.com"))
    assert workspaces.can_view(store, mine.id, "them@example.com")
    assert workspaces.is_shared(store, mine.id)


def test_your_own_workspace_is_not_visible_to_anyone_else_by_default(store):
    workspaces.ensure_user(store, "one@example.com", "One")
    workspaces.ensure_user(store, "two@example.com", "Two")
    ones = workspaces.accessible(store, "one@example.com")[0]
    assert workspaces.can_view(store, ones.id, "two@example.com") is False


def test_the_creator_of_a_workspace_owns_it(store):
    workspace = workspaces.create(store, "Client work", "me@example.com")
    assert workspaces.can_admin(store, workspace.id, "me@example.com")


def test_a_member_is_not_an_owner(store):
    workspace = workspaces.create(store, "Client work", "me@example.com")
    store.add_member(WorkspaceMember(workspace_id=workspace.id, email="them@example.com"))
    assert workspaces.can_view(store, workspace.id, "them@example.com")
    assert not workspaces.can_admin(store, workspace.id, "them@example.com")


def test_a_stranger_has_no_role(store):
    workspace = workspaces.create(store, "Client work", "me@example.com")
    assert workspaces.role_in(store, workspace.id, "nobody@example.com") is None
    assert workspaces.accessible(store, "nobody@example.com") == []


def test_with_no_sso_the_local_operator_owns_everything(store):
    """The dashboard is already unauthenticated there; a fictional user would
    guard nothing."""
    workspace = workspaces.create(store, "Whatever", "someone@example.com")
    assert workspaces.can_admin(store, workspace.id, None, unauthenticated=True)
    assert len(workspaces.accessible(store, None, unauthenticated=True)) >= 1


def test_the_local_operator_gets_a_claiming_workspace_when_there_is_nothing(store):
    only = workspaces.accessible(store, None, unauthenticated=True)[0]
    assert only.claims_unassigned


# --- through the dashboard, with SSO ------------------------------------------------------ #

@pytest.fixture()
def multiuser(monkeypatch, tmp_path):
    """A dashboard with SSO on and two allowed users."""
    auth = AuthSettings(issuer=ISSUER, client_id=CLIENT_ID, client_secret="s",
                        allowed_domains=["example.com"])
    patched = dataclasses.replace(config_module.settings, auth=auth, data_dir=tmp_path)
    for module in (sso, app_module, config_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(sso, "discovery", lambda *a, **kw: DISCOVERY)
    store = LocalStore(db_url=f"sqlite:///{tmp_path/'ws.sqlite'}")
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application, store


def _as(application, email, name="User"):
    client = application.test_client()
    with client.session_transaction() as sess:
        sess[sso._SESSION_USER] = {"email": email, "name": name}
    return client


def test_signing_in_gives_you_exactly_one_workspace(multiuser):
    application, store = multiuser
    _as(application, "one@example.com").get("/")
    mine = workspaces.accessible(store, "one@example.com")
    assert [w.name for w in mine] == ["User's workspace"]


def test_a_new_colleague_sees_an_empty_dashboard(multiuser):
    """Not someone else's live accounts and running instructions."""
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.accessible(store, "one@example.com")[0]
    store.upsert_instruction(Instruction(name="Live campaign", workspace_id=mine.id))

    page = _as(application, "two@example.com", "Two").get("/instructions").get_data(as_text=True)
    assert "Live campaign" not in page


def test_your_workspaces_content_is_invisible_to_a_colleague(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.accessible(store, "one@example.com")[0]
    secret = store.upsert_instruction(Instruction(name="Secret campaign",
                                                  workspace_id=mine.id))

    two = _as(application, "two@example.com", "Two")
    page = two.get("/instructions").get_data(as_text=True)
    assert "Secret campaign" not in page
    # ...and it cannot be reached by guessing the id either.
    assert two.get(f"/instructions/{secret.id}/edit").status_code == 404


def test_another_workspaces_run_is_not_readable(multiuser):
    application, store = multiuser
    _as(application, "one@example.com", "One").get("/")
    mine = workspaces.accessible(store, "one@example.com")[0]
    run = store.add_run(Run(instruction_id="i", account_id="a", workspace_id=mine.id,
                            status=RunStatus.failed, caption="private caption"))
    two = _as(application, "two@example.com", "Two")
    two.get("/")
    assert two.get(f"/runs/{run.id}").status_code == 404
    assert "private caption" not in two.get("/runs").get_data(as_text=True)


def test_a_colleague_cannot_retry_or_republish_someone_elses_run(multiuser):
    """The dangerous pair: both start a real run against an account you cannot see."""
    application, store = multiuser
    _as(application, "one@example.com", "One").get("/")
    mine = workspaces.accessible(store, "one@example.com")[0]
    run = store.add_run(Run(instruction_id="i", account_id="a", workspace_id=mine.id,
                            status=RunStatus.failed, caption="c", asset_path="/a.jpg"))
    two = _as(application, "two@example.com", "Two")
    two.get("/")
    assert two.post(f"/runs/{run.id}/retry", data={"prompt": "x"}).status_code == 404
    assert two.post(f"/runs/{run.id}/republish", data={"caption": "x"}).status_code == 404


def test_an_invited_colleague_sees_the_shared_workspace(multiuser):
    """Sharing works on ANY workspace, including the one you started with."""
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.accessible(store, "one@example.com")[0]
    store.upsert_instruction(Instruction(name="Team campaign", workspace_id=mine.id))
    one.post(f"/workspaces/{mine.id}/members", data={"email": "two@example.com"},
             follow_redirects=True)

    two = _as(application, "two@example.com", "Two")
    two.get("/")
    two.post("/workspaces/switch", data={"workspace_id": mine.id})
    assert "Team campaign" in two.get("/instructions").get_data(as_text=True)


def test_an_invited_colleague_still_LANDS_in_their_own(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.accessible(store, "one@example.com")[0]
    one.post(f"/workspaces/{mine.id}/members", data={"email": "two@example.com"},
             follow_redirects=True)
    two = _as(application, "two@example.com", "Two")
    two.get("/")
    theirs = workspaces.landing(store, "two@example.com")
    assert theirs.created_by == "two@example.com"
    with two.session_transaction() as sess:
        assert sess["workspace_id"] == theirs.id


def test_switching_to_a_workspace_you_are_not_in_is_refused(multiuser):
    application, store = multiuser
    _as(application, "one@example.com", "One").get("/")
    mine = workspaces.accessible(store, "one@example.com")[0]
    two = _as(application, "two@example.com", "Two")
    two.get("/")
    assert two.post(f"/workspaces/{mine.id}/switch").status_code == 404


def test_an_invited_member_cannot_manage_membership(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.accessible(store, "one@example.com")[0]
    one.post(f"/workspaces/{mine.id}/members", data={"email": "two@example.com"},
             follow_redirects=True)
    two = _as(application, "two@example.com", "Two")
    two.get("/")
    assert two.post(f"/workspaces/{mine.id}/members",
                    data={"email": "three@example.com"}).status_code == 403


def test_an_owner_can_add_a_member(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    one.post(f"/workspaces/{mine.id}/members",
             data={"email": "two@example.com", "role": "owner"}, follow_redirects=True)
    assert workspaces.can_admin(store, mine.id, "two@example.com")


def test_adding_someone_outside_the_signin_allowlist_says_so(multiuser):
    """Membership and sign-in are separate gates; silently adding someone who
    can never log in looks like it worked."""
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    page = one.post(f"/workspaces/{mine.id}/members", data={"email": "out@elsewhere.com"},
                    follow_redirects=True).get_data(as_text=True)
    assert "NOT on the sign-in allowlist" in page


def test_the_last_owner_cannot_be_removed(multiuser):
    """A workspace with no owner can never have its membership changed again."""
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    page = one.post(f"/workspaces/{mine.id}/members/remove",
                    data={"email": "one@example.com"},
                    follow_redirects=True).get_data(as_text=True)
    assert "last owner" in page
    assert workspaces.can_admin(store, mine.id, "one@example.com")


def test_a_workspace_with_content_is_not_deleted(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    store.upsert_instruction(Instruction(name="keep me", workspace_id=mine.id))
    page = one.post(f"/workspaces/{mine.id}/delete",
                    follow_redirects=True).get_data(as_text=True)
    assert "still holds" in page
    assert store.get_workspace(mine.id) is not None


def test_an_empty_workspace_can_be_deleted(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    one.post(f"/workspaces/{mine.id}/delete", follow_redirects=True)
    assert store.get_workspace(mine.id) is None
    assert store.list_members(mine.id) == []


def test_a_new_instruction_lands_in_the_current_workspace(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    one.post(f"/workspaces/{mine.id}/switch")
    one.post("/instructions", data={"name": "Fresh", "publish_mode": "dry_run",
                                    "media_pref": "auto"}, follow_redirects=True)
    assert [i.name for i in store.list_instructions(workspace_id=mine.id)] == ["Fresh"]


def test_an_instruction_cannot_target_another_workspaces_account(multiuser):
    """Otherwise a member could publish to an account they cannot even see."""
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    other = workspaces.create(store, "Theirs", "two@example.com")
    theirs = store.upsert_account(Account(platform=PlatformName.instagram, handle="theirs",
                                          external_id="9", workspace_id=other.id))
    one.post(f"/workspaces/{mine.id}/switch")
    one.post("/instructions", data={"name": "Sneaky", "publish_mode": "dry_run",
                                    "media_pref": "auto", "account_ids": theirs.id},
             follow_redirects=True)
    saved = store.list_instructions(workspace_id=mine.id)[0]
    assert saved.account_ids == []


def test_connecting_an_account_is_an_owner_action(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    one.post(f"/workspaces/{mine.id}/members", data={"email": "two@example.com"},
             follow_redirects=True)
    two = _as(application, "two@example.com", "Two")
    two.get("/")
    two.post("/workspaces/switch", data={"workspace_id": mine.id})
    assert two.get("/oauth/instagram/start").status_code == 403


def test_the_counts_on_the_page_match_what_the_page_lists(multiuser):
    """End to end: the workspace that owns pre-existing content must count it."""
    application, store = multiuser
    store.upsert_instruction(Instruction(name="LEGACY-INSTRUCTION"))   # no workspace
    one = _as(application, "one@example.com", "One")
    one.get("/")
    listed = one.get("/instructions").get_data(as_text=True)
    page = one.get("/workspaces").get_data(as_text=True)
    assert "LEGACY-INSTRUCTION" in listed
    assert "1 instruction(s)" in page


def test_the_switcher_cannot_be_used_as_an_open_redirect(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com", "One")
    one.get("/")
    mine = workspaces.accessible(store, "one@example.com")[0]
    resp = one.post("/workspaces/switch",
                    data={"workspace_id": mine.id, "next": "https://evil.example/"})
    assert "evil.example" not in resp.headers["Location"]


# --- without SSO ------------------------------------------------------------------------- #

def test_without_sso_everything_stays_visible(store, monkeypatch, tmp_path):
    """Local development must not need an identity provider to see its own data."""
    patched = dataclasses.replace(config_module.settings,
                                  auth=AuthSettings(), data_dir=tmp_path)
    for module in (sso, app_module, config_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    client = application.test_client()

    store.upsert_instruction(Instruction(name="Legacy instruction"))   # no workspace
    page = client.get("/instructions").get_data(as_text=True)
    assert "Legacy instruction" in page
    # ...and the implicit operator may do owner things.
    assert client.get("/workspaces").status_code == 200

