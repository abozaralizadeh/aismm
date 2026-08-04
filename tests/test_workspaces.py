"""Workspaces: several people on one deployment, sharing some things and not others.

A workspace is a silo — its own accounts, instructions, runs and staged posts.
Partitioning only the instructions would have been less work and would not have
produced a private workspace at all: every member could still have published to
every connected account.

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
    Account, Instruction, PlatformName, Run, RunStatus, StagedPost, WorkspaceKind,
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


def test_the_default_workspace_also_claims_rows_written_before_workspaces_existed(store):
    """The upgrade path. Resolved at READ time, so it cannot be lost by a
    migration that was skipped or interrupted."""
    default = workspaces.ensure_default(store)
    store.upsert_account(Account(platform=PlatformName.instagram, handle="legacy",
                                 external_id="9"))          # no workspace_id
    assert store.list_accounts(workspace_id=default.id) == []
    scoped = store.list_accounts(workspace_id=[default.id, ""])
    assert [a.handle for a in scoped] == ["legacy"]


def test_adopting_orphans_is_available_as_a_tidy_up(store):
    default = workspaces.ensure_default(store)
    store.upsert_account(Account(platform=PlatformName.instagram, external_id="9"))
    store.upsert_instruction(Instruction(name="old"))
    assert workspaces.adopt_orphans(store, default.id) == 2
    assert len(store.list_accounts(workspace_id=default.id)) == 1
    assert workspaces.adopt_orphans(store, default.id) == 0      # idempotent


# --- membership and roles --------------------------------------------------------------- #

def test_a_new_signin_joins_the_shared_workspace_and_gets_a_personal_one(store):
    mine = workspaces.ensure_user(store, "Me@Example.com", "Me")
    kinds = sorted(w.kind.value for w in mine)
    assert kinds == ["personal", "shared"]
    # ...and the email is normalized, or a second login makes a second identity.
    assert all(m.email == "me@example.com" for m in store.list_memberships("me@example.com"))


def test_signing_in_twice_does_not_duplicate_anything(store):
    workspaces.ensure_user(store, "me@example.com", "Me")
    before = len(store.list_workspaces())
    workspaces.ensure_user(store, "me@example.com", "Me")
    assert len(store.list_workspaces()) == before
    assert len(store.list_memberships("me@example.com")) == before


def test_everyone_lands_in_the_same_shared_workspace(store):
    """The migration promise: colleagues see the existing content, not a blank page."""
    workspaces.ensure_user(store, "one@example.com", "One")
    workspaces.ensure_user(store, "two@example.com", "Two")
    shared = [w for w in store.list_workspaces() if w.auto_join]
    assert len(shared) == 1
    assert {m.email for m in store.list_members(shared[0].id)} == {"one@example.com",
                                                                  "two@example.com"}


def test_a_personal_workspace_is_not_joined_by_anyone_else(store):
    workspaces.ensure_user(store, "one@example.com", "One")
    workspaces.ensure_user(store, "two@example.com", "Two")
    personal = [w for w in workspaces.accessible(store, "one@example.com")
                if w.kind is WorkspaceKind.personal][0]
    assert workspaces.can_view(store, personal.id, "two@example.com") is False


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


def test_accessible_creates_a_default_when_there_is_nothing_at_all(store):
    assert workspaces.accessible(store, None, unauthenticated=True)[0].auto_join


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


def test_signing_in_bootstraps_workspaces(multiuser):
    application, store = multiuser
    _as(application, "one@example.com").get("/")
    assert len(workspaces.accessible(store, "one@example.com")) == 2


def test_a_personal_workspaces_content_is_invisible_to_a_colleague(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com")
    one.get("/")                                   # bootstrap
    personal = [w for w in workspaces.accessible(store, "one@example.com")
                if w.kind is WorkspaceKind.personal][0]
    secret = store.upsert_instruction(Instruction(name="Secret campaign",
                                                  workspace_id=personal.id))

    two = _as(application, "two@example.com")
    page = two.get("/instructions").get_data(as_text=True)
    assert "Secret campaign" not in page
    # ...and it cannot be reached by guessing the id either.
    assert two.get(f"/instructions/{secret.id}/edit").status_code == 404


def test_another_workspaces_run_is_not_readable(multiuser):
    application, store = multiuser
    _as(application, "one@example.com").get("/")
    personal = [w for w in workspaces.accessible(store, "one@example.com")
                if w.kind is WorkspaceKind.personal][0]
    run = store.add_run(Run(instruction_id="i", account_id="a", workspace_id=personal.id,
                            status=RunStatus.failed, caption="private caption"))
    two = _as(application, "two@example.com")
    two.get("/")
    assert two.get(f"/runs/{run.id}").status_code == 404
    assert "private caption" not in two.get("/runs").get_data(as_text=True)


def test_a_colleague_cannot_retry_or_republish_someone_elses_run(multiuser):
    """The dangerous pair: both start a real run against an account you cannot see."""
    application, store = multiuser
    _as(application, "one@example.com").get("/")
    personal = [w for w in workspaces.accessible(store, "one@example.com")
                if w.kind is WorkspaceKind.personal][0]
    run = store.add_run(Run(instruction_id="i", account_id="a", workspace_id=personal.id,
                            status=RunStatus.failed, caption="c", asset_path="/a.jpg"))
    two = _as(application, "two@example.com")
    two.get("/")
    assert two.post(f"/runs/{run.id}/retry", data={"prompt": "x"}).status_code == 404
    assert two.post(f"/runs/{run.id}/republish", data={"caption": "x"}).status_code == 404


def test_the_shared_workspace_is_visible_to_everyone(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com")
    one.get("/")
    shared = [w for w in store.list_workspaces() if w.auto_join][0]
    store.upsert_instruction(Instruction(name="Team campaign", workspace_id=shared.id))

    two = _as(application, "two@example.com")
    two.get("/")
    two.post(f"/workspaces/{shared.id}/switch")
    assert "Team campaign" in two.get("/instructions").get_data(as_text=True)


def test_switching_to_a_workspace_you_are_not_in_is_refused(multiuser):
    application, store = multiuser
    _as(application, "one@example.com").get("/")
    personal = [w for w in workspaces.accessible(store, "one@example.com")
                if w.kind is WorkspaceKind.personal][0]
    two = _as(application, "two@example.com")
    two.get("/")
    assert two.post(f"/workspaces/{personal.id}/switch").status_code == 404


def test_a_member_cannot_manage_membership(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com")
    one.get("/")
    shared = [w for w in store.list_workspaces() if w.auto_join][0]
    resp = one.post(f"/workspaces/{shared.id}/members", data={"email": "x@example.com"})
    assert resp.status_code == 403          # auto-joined as a member, not an owner


def test_an_owner_can_add_a_member(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    one.post(f"/workspaces/{mine.id}/members",
             data={"email": "two@example.com", "role": "owner"}, follow_redirects=True)
    assert workspaces.can_admin(store, mine.id, "two@example.com")


def test_adding_someone_outside_the_signin_allowlist_says_so(multiuser):
    """Membership and sign-in are separate gates; silently adding someone who
    can never log in looks like it worked."""
    application, store = multiuser
    one = _as(application, "one@example.com")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    page = one.post(f"/workspaces/{mine.id}/members", data={"email": "out@elsewhere.com"},
                    follow_redirects=True).get_data(as_text=True)
    assert "NOT on the sign-in allowlist" in page


def test_the_last_owner_cannot_be_removed(multiuser):
    """A workspace with no owner can never have its membership changed again."""
    application, store = multiuser
    one = _as(application, "one@example.com")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    page = one.post(f"/workspaces/{mine.id}/members/remove",
                    data={"email": "one@example.com"},
                    follow_redirects=True).get_data(as_text=True)
    assert "last owner" in page
    assert workspaces.can_admin(store, mine.id, "one@example.com")


def test_a_workspace_with_content_is_not_deleted(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    store.upsert_instruction(Instruction(name="keep me", workspace_id=mine.id))
    page = one.post(f"/workspaces/{mine.id}/delete",
                    follow_redirects=True).get_data(as_text=True)
    assert "still holds" in page
    assert store.get_workspace(mine.id) is not None


def test_an_empty_workspace_can_be_deleted(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    one.post(f"/workspaces/{mine.id}/delete", follow_redirects=True)
    assert store.get_workspace(mine.id) is None
    assert store.list_members(mine.id) == []


def test_a_new_instruction_lands_in_the_current_workspace(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com")
    one.get("/")
    mine = workspaces.create(store, "Mine", "one@example.com")
    one.post(f"/workspaces/{mine.id}/switch")
    one.post("/instructions", data={"name": "Fresh", "publish_mode": "dry_run",
                                    "media_pref": "auto"}, follow_redirects=True)
    assert [i.name for i in store.list_instructions(workspace_id=mine.id)] == ["Fresh"]


def test_an_instruction_cannot_target_another_workspaces_account(multiuser):
    """Otherwise a member could publish to an account they cannot even see."""
    application, store = multiuser
    one = _as(application, "one@example.com")
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
    one = _as(application, "one@example.com")
    one.get("/")
    shared = [w for w in store.list_workspaces() if w.auto_join][0]
    one.post(f"/workspaces/{shared.id}/switch")
    assert one.get("/oauth/instagram/start").status_code == 403


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


def test_the_switcher_cannot_be_used_as_an_open_redirect(multiuser):
    application, store = multiuser
    one = _as(application, "one@example.com")
    one.get("/")
    shared = [w for w in store.list_workspaces() if w.auto_join][0]
    resp = one.post("/workspaces/switch",
                    data={"workspace_id": shared.id, "next": "https://evil.example/"})
    assert "evil.example" not in resp.headers["Location"]
