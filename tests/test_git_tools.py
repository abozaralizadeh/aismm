"""Read-only GitHub tools behind a per-instruction PAT (``ProviderConfig`` kind "git").

What must hold, pinned here without a network:

* the token goes ONLY to the connection's ``api_url``, in a header, never the URL —
  and nothing the agent types can steer it elsewhere;
* the optional repository allowlist is enforced in code;
* ``since_sha`` returns only the commits newer than the one saved last run;
* GitHub's refusals come back as advice the agent can act on (a private repo the
  token can't see is a 404, and must not be reported as "doesn't exist");
* output is bounded and every truncation is said;
* there is NO deployment default: no connection picked ⇒ no git tools at all;
* the token round-trips only through the decrypt path, in both backends.
"""
import asyncio
import base64
import dataclasses

import httpx
import pytest

from aismm.config import GitSettings
from aismm.models import Instruction, ProviderConfig
from aismm.tools import git_tools

API = "https://api.github.com"
TOKEN = "github_pat_SECRET123"


def _git(**kw) -> GitSettings:
    return GitSettings(token=TOKEN, api_url=kw.pop("api_url", API), **kw)


@pytest.fixture()
def github(monkeypatch):
    """Route git_tools' httpx through a handler; record every request."""
    calls: list[httpx.Request] = []
    routes: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for (path, status), payload in routes.items():
            if request.url.path == path:
                headers = payload.pop("__headers__", {}) if isinstance(payload, dict) else {}
                return httpx.Response(status, json=payload, headers=headers)
        return httpx.Response(404, json={"message": "Not Found"})

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(git_tools.httpx, "AsyncClient", client)

    class _Hub:
        def route(self, path, payload, status=200):
            routes[(path, status)] = payload

    hub = _Hub()
    hub.calls = calls
    return hub


def _run(coro):
    return asyncio.run(coro)


def _commit(sha, title, date="2026-09-20T10:00:00Z"):
    return {"sha": sha, "html_url": f"https://github.com/me/app/commit/{sha}",
            "author": {"login": "me"},
            "commit": {"message": f"{title}\n\nbody", "author": {"name": "Me", "date": date}}}


# --- where the token goes -------------------------------------------------------------- #

def test_the_token_is_a_header_and_never_in_the_url(github):
    github.route("/repos/me/app/commits", [_commit("a" * 40, "feat: x")])
    _run(git_tools.recent_commits(_git(), "me/app"))

    (req,) = github.calls
    assert req.url.host == "api.github.com"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(req.url)
    assert req.method == "GET"


def test_a_pasted_url_to_another_host_still_goes_to_the_api_url(github):
    """The agent names repos, not hosts — a URL is reduced to owner/name."""
    github.route("/repos/me/app/commits", [])
    _run(git_tools.recent_commits(_git(), "https://evil.example.com/me/app"))
    assert github.calls[0].url.host == "api.github.com"
    assert github.calls[0].url.path == "/repos/me/app/commits"


@pytest.mark.parametrize("repo", ["../../etc", "me", "me/app/extra", "me/..", "me app/x", ""])
def test_malformed_repos_are_refused_before_any_request(github, repo):
    got = _run(git_tools._run({"git_settings": _git()},
                              lambda g: git_tools.recent_commits(g, repo)))
    assert got["error"] == "bad_repo"
    assert github.calls == []


@pytest.mark.parametrize("path", ["../secrets", "a/../../b", "./x", "a\\b"])
def test_path_traversal_is_refused(github, path):
    got = _run(git_tools._run({"git_settings": _git()},
                              lambda g: git_tools.read_file(g, "me/app", path)))
    assert got["error"] == "bad_path"
    assert github.calls == []


def test_a_bad_ref_is_refused(github):
    got = _run(git_tools._run({"git_settings": _git()},
                              lambda g: git_tools.commit_detail(g, "me/app", "a..b")))
    assert got["error"] == "bad_ref"
    assert github.calls == []


# --- the allowlist --------------------------------------------------------------------- #

def test_the_allowlist_refuses_other_repos(github):
    git = _git(repos=("me/app",))
    got = _run(git_tools._run({"git_settings": git},
                              lambda g: git_tools.recent_commits(g, "me/secret")))
    assert got["error"] == "repo_not_allowed"
    assert github.calls == []


def test_owner_wildcard_allows_the_whole_owner():
    git = _git(repos=("my-org/*",))
    assert git.allows("my-org/anything") and git.allows("MY-ORG/Thing")
    assert not git.allows("someone/else")
    assert GitSettings(token="t").allows("any/repo")      # empty list = everything


def test_list_repos_is_filtered_by_owner_and_allowlist(github):
    github.route("/user/repos", [
        {"full_name": "me/app", "private": True, "pushed_at": "2026-09-20"},
        {"full_name": "me/other", "private": True},
        {"full_name": "org/lib", "private": False},
    ])
    got = _run(git_tools.list_repos(_git(repos=("me/app", "org/lib"))))
    assert [r["repo"] for r in got["repos"]] == ["me/app", "org/lib"]
    assert got["repos"][0]["private"] is True
    got = _run(git_tools.list_repos(_git(), owner="org"))
    assert [r["repo"] for r in got["repos"]] == ["org/lib"]


# --- "what is new since last run" ------------------------------------------------------ #

def test_since_sha_returns_only_newer_commits(github):
    old = "c" * 40
    github.route("/repos/me/app/commits",
                 [_commit("a" * 40, "feat: new"), _commit("b" * 40, "fix: newer"),
                  _commit(old, "already posted"), _commit("d" * 40, "older")])
    got = _run(git_tools.recent_commits(_git(), "me/app", since_sha=old[:7]))
    assert [c["title"] for c in got["commits"]] == ["feat: new", "fix: newer"]
    assert got["newest_sha"] == "a" * 40
    assert "note" not in got


def test_since_sha_not_found_says_there_may_be_a_gap(github):
    github.route("/repos/me/app/commits", [_commit("a" * 40, "feat")])
    got = _run(git_tools.recent_commits(_git(), "me/app", since_sha="f" * 40))
    assert "may be more in between" in got["note"]


def test_nothing_new_keeps_the_saved_sha(github):
    saved = "a" * 40
    github.route("/repos/me/app/commits", [_commit(saved, "last")])
    got = _run(git_tools.recent_commits(_git(), "me/app", since_sha=saved))
    assert got["commits"] == [] and got["newest_sha"] == saved
    assert got["note"] == "No new commits."


# --- refusals the agent can act on ----------------------------------------------------- #

def test_404_says_it_may_just_be_invisible_to_the_token(github):
    got = _run(git_tools._run({"git_settings": _git()},
                              lambda g: git_tools.recent_commits(g, "me/private")))
    assert got["error"] == "not_found"
    assert "no access" in got["message"] and "OR" in got["message"]


def test_401_says_replace_the_token(github):
    github.route("/user/repos", {"message": "Bad credentials"}, status=401)
    got = _run(git_tools._run({"git_settings": _git()}, lambda g: git_tools.list_repos(g)))
    assert got["error"] == "unauthorized"
    assert "Settings" in got["message"] and TOKEN not in got["message"]


def test_rate_limit_is_named(github):
    github.route("/user/repos", {"message": "API rate limit exceeded",
                                 "__headers__": {"x-ratelimit-remaining": "0",
                                                 "x-ratelimit-reset": "1790000000"}},
                 status=403)
    got = _run(git_tools._run({"git_settings": _git()}, lambda g: git_tools.list_repos(g)))
    assert got["error"] == "rate_limited"


def test_plain_403_explains_fine_grained_permissions(github):
    github.route("/repos/me/app/pulls", {"message": "Resource not accessible"}, status=403)
    got = _run(git_tools._run({"git_settings": _git()},
                              lambda g: git_tools.pull_requests(g, "me/app")))
    assert got["error"] == "forbidden" and "Pull requests: read" in got["message"]


# --- bounded output -------------------------------------------------------------------- #

def test_read_file_decodes_and_says_when_it_truncates(github):
    text = "x" * (git_tools._MAX_FILE_CHARS + 10)
    github.route("/repos/me/app/contents/README.md",
                 {"type": "file", "encoding": "base64", "size": len(text),
                  "content": base64.b64encode(text.encode()).decode()})
    got = _run(git_tools.read_file(_git(), "me/app", "README.md"))
    assert len(got["content"]) == git_tools._MAX_FILE_CHARS
    assert got["truncated"] is True and "first" in got["note"]


def test_read_file_lists_a_directory_and_refuses_binary(github):
    github.route("/repos/me/app/contents/", [{"name": "src", "path": "src", "type": "dir"}])
    github.route("/repos/me/app/contents/logo.png",
                 {"type": "file", "encoding": "base64", "size": 4,
                  "content": base64.b64encode(b"\x89PN\x00").decode()})
    listing = _run(git_tools.read_file(_git(), "me/app", ""))
    assert listing["type"] == "dir" and listing["entries"][0]["name"] == "src"
    binary = _run(git_tools.read_file(_git(), "me/app", "logo.png"))
    assert "Binary" in binary["note"] and "content" not in binary


def test_commit_patches_are_truncated_and_flagged(github):
    sha = "a" * 40
    big = "+" * (git_tools._MAX_PATCH_CHARS + 50)
    github.route(f"/repos/me/app/commits/{sha}",
                 {**_commit(sha, "feat: big"), "stats": {"additions": 9, "deletions": 1},
                  "files": [{"filename": "a.py", "status": "modified", "additions": 9,
                             "deletions": 1, "patch": big}]})
    got = _run(git_tools.commit_detail(_git(), "me/app", sha))
    f = got["files"][0]
    assert len(f["patch"]) == git_tools._MAX_PATCH_CHARS and f["patch_truncated"]


def test_compare_defaults_head_to_the_default_branch(github):
    github.route("/repos/me/app", {"default_branch": "main"})
    github.route("/repos/me/app/compare/abc1234...main",
                 {"status": "ahead", "ahead_by": 1, "total_commits": 1,
                  "commits": [_commit("f" * 40, "feat: y")], "files": []})
    got = _run(git_tools.compare(_git(), "me/app", "abc1234"))
    assert got["head"] == "main" and got["newest_sha"] == "f" * 40


# --- the tools exist only with a connection -------------------------------------------- #

def test_no_connection_means_no_git_tools():
    from aismm.tools import registry
    names = [n for n in registry.registered_tool_names() if n.startswith("git_")]
    assert len(names) == 6
    for name in names:
        factory = registry._TOOL_FACTORIES[name]
        assert factory({}) is None
        assert factory({"git_settings": GitSettings()}) is None     # no token
        tool = factory({"git_settings": _git()})
        assert tool is not None
        # The description must survive the SDK's docstring parser, AND carry the
        # privacy note — the model acts on this text.
        assert len(tool.description) > len(git_tools._PRIVATE_NOTE) + 40
        assert "PRIVATE" in tool.description


# --- storage: the token only comes back through the decrypt path ------------------------ #

def _both_backends():
    from aismm.store.azure_store import AzureStore
    from aismm.store.local_store import LocalStore
    from tests.test_azure_store import FakeTableClient

    return [("local", LocalStore(db_url="sqlite:///:memory:")),
            ("azure", AzureStore(table_client=FakeTableClient()))]


_BOTH = pytest.mark.parametrize("label,store", _both_backends(),
                                ids=lambda v: v if isinstance(v, str) else "")


@_BOTH
def test_git_config_round_trips_and_never_leaks_the_token(label, store):
    store.init()
    cfg = ProviderConfig(kind="git", created_by="me@x.com", name="Private repos")
    cfg.set_config({"api_url": API, "repos_csv": "me/app, org/*"})
    cfg = store.upsert_provider_config(cfg, secrets={"token": TOKEN})

    listed = store.list_provider_configs(kind="git")
    assert len(listed) == 1
    assert TOKEN not in listed[0].config_json and TOKEN not in listed[0].secrets_enc

    git = store.resolve_git_settings(cfg.id)
    assert git.token == TOKEN and git.api_url == API
    assert git.repos == ("me/app", "org/*")

    cfg.enabled = False
    store.upsert_provider_config(cfg)
    assert store.resolve_git_settings(cfg.id) is None
    assert store.resolve_git_settings("") is None


@_BOTH
def test_an_image_config_is_not_a_git_config(label, store):
    store.init()
    img = store.upsert_provider_config(ProviderConfig(kind="image", created_by="me@x.com"),
                                       secrets={"api_key": "k"})
    assert store.resolve_git_settings(img.id) is None


@_BOTH
def test_instruction_carries_the_git_pick(label, store):
    store.init()
    instr = store.upsert_instruction(Instruction(name="I", git_config_id="git-1"))
    assert store.get_instruction(instr.id).git_config_id == "git-1"


def test_run_resolution_has_no_deployment_default(store):
    from aismm.agent import manager_agent

    from aismm.models import Workspace

    store.init()
    ws = store.upsert_workspace(Workspace(name="Mine", created_by="me@x.com"))
    assert manager_agent._resolve_git(Instruction(name="I", workspace_id=ws.id), store) is None
    cfg = store.upsert_provider_config(
        ProviderConfig(kind="git", created_by="me@x.com", workspace_id=ws.id),
        secrets={"token": TOKEN})
    got = manager_agent._resolve_git(
        Instruction(name="I", workspace_id=ws.id, git_config_id=cfg.id), store)
    assert isinstance(got, GitSettings) and got.token == TOKEN

    # Someone else's private connection is NOT usable just by knowing its id.
    theirs = store.upsert_provider_config(
        ProviderConfig(kind="git", created_by="stranger@x.com", workspace_id="w-other"),
        secrets={"token": "theirs"})
    assert manager_agent._resolve_git(
        Instruction(name="I", workspace_id=ws.id, git_config_id=theirs.id), store) is None


# --- the dashboard ------------------------------------------------------------------------ #

@pytest.fixture()
def app_client(store, monkeypatch, tmp_path):
    from aismm import assets as assets_module
    from aismm import config as config_module
    from aismm.config import AuthSettings
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    (tmp_path / "assets").mkdir(exist_ok=True)
    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module, assets_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    store.init()
    application = app_module.create_app()
    application.secret_key = "test"
    return application.test_client()


def test_settings_saves_a_git_connection_without_echoing_the_token(app_client, store):
    resp = app_client.post("/settings/provider/git", data={
        "name": "Mine", "token": TOKEN, "repos": "me/app\norg/*", "enabled": "on",
        "api_url": API})
    assert resp.status_code == 302
    (cfg,) = store.list_provider_configs(kind="git")
    assert store.resolve_git_settings(cfg.id).repos == ("me/app", "org/*")

    page = app_client.get("/settings").get_data(as_text=True)
    assert "Git connections" in page and "Mine" in page
    assert TOKEN not in page

    form = app_client.get("/instructions/new").get_data(as_text=True)
    assert 'name="git_config_id"' in form and "Mine" in form


def test_settings_refuses_a_plain_http_api_url(app_client, store):
    app_client.post("/settings/provider/git", data={
        "name": "Bad", "token": TOKEN, "api_url": "http://ghe.local/api/v3"})
    assert store.list_provider_configs(kind="git") == []


def test_settings_refuses_a_malformed_repo_entry(app_client, store):
    app_client.post("/settings/provider/git", data={
        "name": "Bad", "token": TOKEN, "repos": "just-a-name"})
    assert store.list_provider_configs(kind="git") == []
