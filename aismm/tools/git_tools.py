"""Read-only GitHub tools: explore repositories — private ones included — with a PAT.

The use case: "watch my repos and write a post about what I've been building". For
public repos the agent could always `browse_page` github.com; a PRIVATE repo is a 404
to anyone not signed in, so it needs a token. The token is a ``ProviderConfig`` of
kind ``"git"`` (Settings → Git connections, Fernet-encrypted like every other
secret), picked per instruction; with none picked, every factory here returns
``None`` and the run never sees these tools.

Four rules keep this safe to hand an autonomous agent:

* **READ ONLY.** Every call is a ``GET``. There is no write path to add a bug to.
* **The token only ever goes to the operator's ``api_url``.** The agent names
  ``owner/repo``, never a URL; each piece it supplies is validated and URL-quoted, so
  it cannot steer the ``Authorization`` header at another host or another endpoint.
  Header only, never the query string (httpx prints URLs in its exceptions).
* **The optional allowlist is enforced here, in code** (``GitSettings.allows``) —
  a fine-grained PAT is the real boundary, but an instruction about ONE project
  should not be able to wander into the others the same token can read.
* **Output is bounded.** Diffs and files are truncated, so one huge commit cannot
  flood the context window, and every truncation is SAID, so the agent never
  mistakes a cut file for the whole of it.

GitHub answers 404 — not 403 — for a private repo the token cannot see, so a 404
is reported as "missing OR not visible to this token", never as "does not exist".
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from agents import function_tool

from ..config import GitSettings
from .registry import register_tool

logger = logging.getLogger("aismm.tools.git")

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REF_RE = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")   # branch, tag or sha

_TIMEOUT = 30.0
_MAX_PATCH_CHARS = 2500      # per file in a commit/compare
_MAX_FILES = 40              # files listed per commit/compare
_MAX_FILE_CHARS = 20000      # git_read_file
_MAX_DIR_ENTRIES = 200

# The one line every tool docstring carries: these repos can be private, and the
# output is written for a PUBLIC post.
_PRIVATE_NOTE = (
    "The repository may be PRIVATE: describe what changed and why it matters, but "
    "never paste source code, secrets, keys, internal hostnames or unreleased "
    "customer names into a post."
)


def _settings(state: dict) -> GitSettings | None:
    got = state.get("git_settings")
    return got if isinstance(got, GitSettings) and got.enabled else None


def _headers(git: GitSettings) -> dict:
    return {
        "Authorization": f"Bearer {git.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aismm-git-tools",
    }


class GitError(Exception):
    """A GitHub refusal translated into something the agent can act on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


def _check_repo(git: GitSettings, repo: str) -> str:
    repo = (repo or "").strip().strip("/")
    if repo.lower().startswith(("https://", "http://")):
        # Accept a pasted github.com URL rather than failing on a formality.
        parts = repo.split("://", 1)[1].split("/")
        repo = "/".join(parts[1:3]) if len(parts) >= 3 else ""
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _REPO_RE.match(repo) or ".." in repo:
        raise GitError("bad_repo", f"Expected a repository as 'owner/name', got {repo!r}.")
    if not git.allows(repo):
        raise GitError("repo_not_allowed",
                       f"{repo} is not in this Git connection's repository list "
                       f"({', '.join(git.repos)}). Work with one of those.")
    return repo


def _check_ref(ref: str, what: str = "ref") -> str:
    ref = (ref or "").strip()
    if ref and (not _REF_RE.match(ref) or ".." in ref):
        raise GitError("bad_ref", f"{what} {ref!r} is not a valid branch, tag or commit sha.")
    return ref


def _explain(status: int, body: str, resp_headers, what: str) -> GitError:
    """Name the fix, not just the status — the agent acts on this message."""
    detail = ""
    try:
        import json
        detail = (json.loads(body) or {}).get("message", "")
    except Exception:  # noqa: BLE001 - body may be HTML or empty
        detail = (body or "")[:200]
    if status == 401:
        return GitError("unauthorized",
                        "GitHub rejected the token (401): it is wrong, revoked or expired. "
                        "The operator has to paste a new one in Settings → Git connections. "
                        "Retrying will not help.")
    remaining = (resp_headers or {}).get("x-ratelimit-remaining")
    if status in (403, 429) and (remaining == "0" or "rate limit" in detail.lower()):
        reset = (resp_headers or {}).get("x-ratelimit-reset", "")
        when = ""
        if reset.isdigit():
            when = " until " + datetime.fromtimestamp(int(reset), timezone.utc).strftime(
                "%H:%M UTC")
        return GitError("rate_limited",
                        f"GitHub rate limit reached{when}. Work with what you already read "
                        "instead of reading more.")
    if status == 403:
        return GitError("forbidden",
                        f"GitHub refused {what} (403: {detail or 'forbidden'}). A fine-grained "
                        "token needs this repository selected and 'Contents: read' (plus "
                        "'Pull requests: read' for pull requests); an organisation may also "
                        "have to approve the token.")
    if status == 404:
        return GitError("not_found",
                        f"{what} was not found (404). For a PRIVATE repository GitHub answers "
                        "404 when the token cannot see it, so this means it does not exist "
                        "OR this token has no access to it. Check the name with "
                        "git_list_repos.")
    if status == 409:
        return GitError("empty_repository", f"{what}: the repository is empty (409).")
    if status == 422:
        return GitError("invalid", f"GitHub could not process {what} (422: {detail}).")
    return GitError("github_error", f"GitHub answered {status} for {what}: {detail}")


async def _get(git: GitSettings, path: str, *, params: dict | None = None,
               what: str = "the request") -> tuple[object, dict]:
    """GET ``{api_url}{path}``. ``path`` is always built here from validated parts."""
    url = f"{git.api_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, params=params, headers=_headers(git))
    except httpx.HTTPError as exc:
        raise GitError("unreachable", f"Could not reach GitHub ({type(exc).__name__}). "
                                      "Try again later.") from None
    if resp.status_code >= 400:
        raise _explain(resp.status_code, resp.text, resp.headers, what)
    return resp.json(), resp.headers


def _short(sha: str) -> str:
    return (sha or "")[:7]


def _first_line(message: str) -> str:
    return (message or "").strip().split("\n", 1)[0][:200]


def _commit_row(item: dict) -> dict:
    commit = item.get("commit") or {}
    author = commit.get("author") or {}
    login = (item.get("author") or {}).get("login", "")
    return {
        "sha": item.get("sha", ""),
        "short_sha": _short(item.get("sha", "")),
        "title": _first_line(commit.get("message", "")),
        "message": (commit.get("message") or "")[:2000],
        "author": login or author.get("name", ""),
        "date": author.get("date", ""),
        "url": item.get("html_url", ""),
    }


def _file_rows(files: list) -> tuple[list[dict], bool]:
    rows = []
    for f in (files or [])[:_MAX_FILES]:
        patch = f.get("patch") or ""
        row = {"filename": f.get("filename", ""), "status": f.get("status", ""),
               "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)}
        if patch:
            row["patch"] = patch[:_MAX_PATCH_CHARS]
            if len(patch) > _MAX_PATCH_CHARS:
                row["patch_truncated"] = True
        elif f.get("status") != "removed":
            row["patch"] = ""
            row["note"] = "no diff shown (binary or too large)"
        rows.append(row)
    return rows, len(files or []) > _MAX_FILES


# --- the operations (plain coroutines, so the tests can call them) ----------------- #

async def list_repos(git: GitSettings, *, owner: str = "", limit: int = 30) -> dict:
    limit = max(1, min(int(limit or 30), 100))
    data, _h = await _get(git, "/user/repos",
                          params={"sort": "pushed", "direction": "desc", "per_page": 100,
                                  "affiliation": "owner,collaborator,organization_member"},
                          what="the repository list")
    owner = (owner or "").strip().lower()
    repos = []
    for r in data or []:
        full = r.get("full_name", "")
        if owner and full.split("/", 1)[0].lower() != owner:
            continue
        if not git.allows(full):
            continue
        repos.append({
            "repo": full, "private": bool(r.get("private")),
            "description": (r.get("description") or "")[:300],
            "default_branch": r.get("default_branch", ""),
            "pushed_at": r.get("pushed_at", ""), "language": r.get("language") or "",
            "stars": r.get("stargazers_count", 0), "url": r.get("html_url", ""),
        })
        if len(repos) >= limit:
            break
    return {"repos": repos, "count": len(repos),
            "note": "Newest push first. Only repositories this token can see are listed."}


async def recent_commits(git: GitSettings, repo: str, *, branch: str = "",
                         since_sha: str = "", since_hours: int = 0, limit: int = 20) -> dict:
    repo = _check_repo(git, repo)
    branch = _check_ref(branch, "branch")
    since_sha = _check_ref(since_sha, "since_sha")
    limit = max(1, min(int(limit or 20), 100))
    params: dict = {"per_page": 100 if since_sha else limit}
    if branch:
        params["sha"] = branch
    if since_hours and int(since_hours) > 0:
        since = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
        params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    data, _h = await _get(git, f"/repos/{repo}/commits", params=params,
                          what=f"commits of {repo}")
    commits, reached = [], not since_sha
    for item in data or []:
        sha = item.get("sha", "")
        if since_sha and (sha == since_sha or sha.startswith(since_sha)):
            reached = True
            break
        commits.append(_commit_row(item))
    result = {"repo": repo, "branch": branch or "(default branch)", "commits": commits[:limit],
              "count": len(commits[:limit]),
              "newest_sha": commits[0]["sha"] if commits else (since_sha or "")}
    if since_sha and not reached:
        result["note"] = (f"{since_sha} was not among the latest {len(data or [])} commits, "
                          "so these are the newest ones — there may be more in between.")
    elif len(commits) > limit:
        result["note"] = f"{len(commits)} new commits; showing the newest {limit}."
    if not commits:
        result["note"] = "No new commits." if since_sha or since_hours else "No commits."
    result["hint"] = ("Record newest_sha with update_memory and pass it as since_sha next "
                      "run to see only what is new.")
    return result


async def commit_detail(git: GitSettings, repo: str, sha: str) -> dict:
    repo = _check_repo(git, repo)
    sha = _check_ref(sha, "sha")
    if not sha:
        raise GitError("bad_ref", "Pass the sha of the commit to read.")
    data, _h = await _get(git, f"/repos/{repo}/commits/{quote(sha, safe='')}",
                          what=f"commit {sha} of {repo}")
    row = _commit_row(data or {})
    files, more = _file_rows((data or {}).get("files") or [])
    stats = (data or {}).get("stats") or {}
    result = {**row, "repo": repo, "additions": stats.get("additions", 0),
              "deletions": stats.get("deletions", 0), "files": files}
    if more:
        result["note"] = f"Only the first {_MAX_FILES} changed files are shown."
    return result


async def compare(git: GitSettings, repo: str, base: str, head: str = "") -> dict:
    repo = _check_repo(git, repo)
    base = _check_ref(base, "base")
    head = _check_ref(head, "head")
    if not base:
        raise GitError("bad_ref", "Pass base (the older sha/branch/tag).")
    if not head:
        info, _h = await _get(git, f"/repos/{repo}", what=repo)
        head = (info or {}).get("default_branch") or "HEAD"
    data, _h = await _get(git, f"/repos/{repo}/compare/{quote(base, safe='/')}..."
                               f"{quote(head, safe='/')}",
                          what=f"{base}...{head} in {repo}")
    data = data or {}
    commits = [_commit_row(c) for c in data.get("commits") or []]
    files, more = _file_rows(data.get("files") or [])
    result = {"repo": repo, "base": base, "head": head, "status": data.get("status", ""),
              "ahead_by": data.get("ahead_by", 0), "total_commits": data.get("total_commits", 0),
              "commits": commits[-50:], "files": files,
              "newest_sha": commits[-1]["sha"] if commits else ""}
    notes = []
    if len(commits) > 50:
        notes.append("Only the newest 50 commits are listed.")
    if more:
        notes.append(f"Only the first {_MAX_FILES} changed files are shown.")
    if notes:
        result["note"] = " ".join(notes)
    return result


async def read_file(git: GitSettings, repo: str, path: str = "", ref: str = "") -> dict:
    repo = _check_repo(git, repo)
    ref = _check_ref(ref)
    path = (path or "").strip().strip("/")
    if any(part in ("..", ".") for part in path.split("/") if part) or "\\" in path:
        raise GitError("bad_path", f"{path!r} is not a repository path.")
    params = {"ref": ref} if ref else None
    data, _h = await _get(git, f"/repos/{repo}/contents/{quote(path, safe='/')}",
                          params=params, what=f"{path or '/'} in {repo}")
    if isinstance(data, list):              # a directory
        entries = [{"name": e.get("name", ""), "path": e.get("path", ""),
                    "type": e.get("type", ""), "size": e.get("size", 0)}
                   for e in data[:_MAX_DIR_ENTRIES]]
        out = {"repo": repo, "path": path or "/", "type": "dir", "entries": entries}
        if len(data) > _MAX_DIR_ENTRIES:
            out["note"] = f"Only the first {_MAX_DIR_ENTRIES} entries are shown."
        return out
    data = data or {}
    if data.get("type") != "file":
        return {"repo": repo, "path": path, "type": data.get("type", "unknown"),
                "note": "Not a regular file (symlink or submodule)."}
    raw = data.get("content") or ""
    try:
        blob = base64.b64decode(raw) if data.get("encoding") == "base64" else raw.encode()
    except (ValueError, TypeError):
        blob = b""
    if not blob and data.get("size"):
        return {"repo": repo, "path": path, "type": "file", "size": data.get("size", 0),
                "note": "File is too large for the contents API (over 1 MB); not read."}
    if b"\x00" in blob[:8000]:
        return {"repo": repo, "path": path, "type": "file", "size": len(blob),
                "note": "Binary file; not shown."}
    text = blob.decode("utf-8", errors="replace")
    out = {"repo": repo, "path": path, "type": "file", "size": len(blob),
           "content": text[:_MAX_FILE_CHARS], "url": data.get("html_url", "")}
    if len(text) > _MAX_FILE_CHARS:
        out["truncated"] = True
        out["note"] = f"Showing the first {_MAX_FILE_CHARS} of {len(text)} characters."
    return out


async def pull_requests(git: GitSettings, repo: str, *, state: str = "closed",
                        limit: int = 15) -> dict:
    repo = _check_repo(git, repo)
    state = state if state in ("open", "closed", "all") else "closed"
    limit = max(1, min(int(limit or 15), 50))
    data, _h = await _get(git, f"/repos/{repo}/pulls",
                          params={"state": state, "sort": "updated", "direction": "desc",
                                  "per_page": limit},
                          what=f"pull requests of {repo}")
    prs = []
    for pr in data or []:
        prs.append({
            "number": pr.get("number"), "title": (pr.get("title") or "")[:200],
            "state": pr.get("state", ""), "merged": bool(pr.get("merged_at")),
            "merged_at": pr.get("merged_at") or "", "updated_at": pr.get("updated_at", ""),
            "author": (pr.get("user") or {}).get("login", ""),
            "body": (pr.get("body") or "")[:1500], "url": pr.get("html_url", ""),
        })
    return {"repo": repo, "state": state, "pull_requests": prs, "count": len(prs)}


# --- tool factories ------------------------------------------------------------------ #

async def _run(state: dict, call) -> dict:
    git = _settings(state)
    if git is None:
        return {"error": "not_available", "message": "No Git connection is set for this run."}
    try:
        return await call(git)
    except GitError as exc:
        logger.info("git tool refused: %s", exc.message)
        return exc.as_dict()
    except Exception as exc:  # noqa: BLE001 - report, never kill the run
        logger.warning("git tool failed: %s", exc)
        return {"error": "git_failed", "message": f"{type(exc).__name__}: {exc}"}


def _tool(fn, doc: str):
    """Wrap ``fn`` as a tool described by ``doc`` + the privacy note.

    Passed as ``description_override`` rather than through ``__doc__``: the SDK
    runs docstrings through a style auto-detector, and a plain sentence like "One
    commit in detail: full message, …" was taken for a section header and DROPPED,
    leaving the model a description of nothing but the note.
    """
    text = " ".join(doc.split())
    return function_tool(fn, description_override=f"{text}\n\n{_PRIVATE_NOTE}")


def _make_list_repos(state: dict):
    if _settings(state) is None:
        return None

    async def git_list_repos(owner: str = "", limit: int = 30) -> dict:
        return await _run(state, lambda g: list_repos(g, owner=owner, limit=limit))

    return _tool(git_list_repos, """
        List the Git repositories you can read (private ones included), most recently
        pushed first. `owner` narrows to one user/organisation. Start here to find
        what has been worked on lately.""")


def _make_recent_commits(state: dict):
    if _settings(state) is None:
        return None

    async def git_recent_commits(repo: str, branch: str = "", since_sha: str = "",
                                 since_hours: int = 0, limit: int = 20) -> dict:
        return await _run(state, lambda g: recent_commits(
            g, repo, branch=branch, since_sha=since_sha, since_hours=since_hours,
            limit=limit))

    return _tool(git_recent_commits, """
        Recent commits of `repo` ('owner/name'), newest first: sha, title, full message,
        author, date. To see only what is NEW since your last run, pass since_sha = the
        newest_sha you saved in memory last time (and save the new newest_sha after
        publishing); since_hours is the alternative. `branch` defaults to the default
        branch.""")


def _make_commit(state: dict):
    if _settings(state) is None:
        return None

    async def git_commit(repo: str, sha: str) -> dict:
        return await _run(state, lambda g: commit_detail(g, repo, sha))

    return _tool(git_commit, """
        One commit in detail: full message, stats and the changed files with a
        (truncated) diff each. Use it to understand WHAT a commit actually did when its
        message is terse.""")


def _make_compare(state: dict):
    if _settings(state) is None:
        return None

    async def git_compare(repo: str, base: str, head: str = "") -> dict:
        return await _run(state, lambda g: compare(g, repo, base, head))

    return _tool(git_compare, """
        Everything that changed between two points — `base` and `head` are shas,
        branches or tags (`head` defaults to the default branch): the commits in between
        and the changed files with diffs. The quickest way to summarise all the work
        since the sha you saved last run, or between two releases.""")


def _make_read_file(state: dict):
    if _settings(state) is None:
        return None

    async def git_read_file(repo: str, path: str = "", ref: str = "") -> dict:
        return await _run(state, lambda g: read_file(g, repo, path, ref))

    return _tool(git_read_file, """
        Read a file (README.md, CHANGELOG.md, docs, …) or list a directory (`path` empty
        = the repository root) at `ref` (default branch when empty). Use it for the
        context a commit message lacks — what the project IS.""")


def _make_pull_requests(state: dict):
    if _settings(state) is None:
        return None

    async def git_pull_requests(repo: str, state_filter: str = "closed",
                                limit: int = 15) -> dict:
        return await _run(state, lambda g: pull_requests(g, repo, state=state_filter,
                                                         limit=limit))

    return _tool(git_pull_requests, """
        Pull requests of `repo`, most recently updated first, with title, description
        and whether/when they were merged. state_filter: "closed" (default — includes
        merged), "open" or "all". A merged PR's description is often the best summary
        of a feature.""")


register_tool("git_list_repos", _make_list_repos)
register_tool("git_recent_commits", _make_recent_commits)
register_tool("git_commit", _make_commit)
register_tool("git_compare", _make_compare)
register_tool("git_read_file", _make_read_file)
register_tool("git_pull_requests", _make_pull_requests)
