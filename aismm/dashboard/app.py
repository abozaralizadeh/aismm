"""The AISMM dashboard (Flask).

This is the control center: connect social accounts via OAuth, author Instructions
(select accounts + brief + schedule + publish mode), and review/approve posts.
Async platform/agent calls are driven from sync routes via ``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import re
import threading
from collections.abc import Callable

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, send_file,
    send_from_directory, session, url_for,
)
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename

from ..config import settings
from ..assets import browser_url, public_url
from .. import attachments, cooldown, tokens, workspaces
from ..agent.prompts import MANAGER_INSTRUCTIONS
from ..assets import save_bytes
from ..models import (
    Account, AttachmentPurpose, Instruction, InstructionFile, InstructionTask, MediaPref,
    PlatformApp, PlatformName, PublishMode, RunStatus, WorkspaceMember, WorkspaceRole,
)
from ..platforms import apps as platform_apps
from ..platforms import setup_guides
from ..platforms.registry import get_platform
from ..schedules import describe as describe_schedule
from ..auth import oauth
from . import sso
from .. import orchestrator, scheduler
from ..store import get_store


class ReverseProxyPrefixMiddleware:
    """Mount a WSGI app at a fixed prefix, whether nginx strips it or not."""

    def __init__(self, app: Callable, prefix: str):
        self.app = app
        self.prefix = prefix

    def __call__(self, environ: dict, start_response: Callable):
        path = environ.get("PATH_INFO", "")
        if path == self.prefix:
            environ["PATH_INFO"] = "/"
        elif path.startswith(f"{self.prefix}/"):
            environ["PATH_INFO"] = path[len(self.prefix):]

        script_name = environ.get("SCRIPT_NAME", "").rstrip("/")
        if not (script_name == self.prefix or script_name.endswith(self.prefix)):
            environ["SCRIPT_NAME"] = f"{script_name}{self.prefix}"
        return self.app(environ, start_response)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = settings.dashboard.secret_key
    prefix = settings.dashboard.reverse_proxy_prefix
    if prefix:
        # APPLICATION_ROOT covers cookies and URL generation outside requests;
        # the middleware sets SCRIPT_NAME for normal proxied requests.
        app.config["APPLICATION_ROOT"] = prefix
        app.wsgi_app = ReverseProxyPrefixMiddleware(app.wsgi_app, prefix)

    # SSO guard + /login, /auth/callback, /logout. Registered before the routes
    # below so every one of them is behind the session check (except /assets,
    # which Instagram must be able to fetch — see sso.PUBLIC_ENDPOINTS).
    sso.init_app(app, settings)

    # The setup guides are written with light markdown so they stay readable in
    # the source file; render just bold / italic / code, escaping first.
    _MD_PATTERNS = (
        (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
        (re.compile(r"`(.+?)`"), r"<code>\1</code>"),
        (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"<em>\1</em>"),
    )

    @app.template_filter("md_inline")
    def _md_inline(text):
        rendered = escape(text or "")
        for pattern, replacement in _MD_PATTERNS:
            rendered = pattern.sub(replacement, str(rendered))
        return Markup(rendered)

    # ---- helpers --------------------------------------------------------- #
    def _platforms_view():
        store = get_store()
        workspace_id = _platform_app_scope()
        allow_env = workspaces.can_use_deployment_config(store, _workspace())
        view = []
        for p in PlatformName:
            view.append({"name": p.value,
                         "configured": platform_apps.is_configured(
                             p, store, workspace_id, allow_env=allow_env),
                         "options": platform_apps.connection_options(
                             p, store, workspace_id, allow_env=allow_env),
                         "capabilities": get_platform(p).capabilities})
        return view

    # ---- workspaces ------------------------------------------------------- #
    # Every scoped query goes through _workspace(); a route that forgets it
    # shows another workspace's data, so these are deliberately short and used
    # everywhere rather than each route filtering by hand.
    _WORKSPACE_KEY = "workspace_id"

    def _identity() -> tuple[str, str, bool]:
        """``(email, display_name, unauthenticated)``.

        With SSO off the dashboard has no identity at all and is already
        unauthenticated — so rather than invent a user, one implicit local
        operator owns everything. See aismm/workspaces.py.
        """
        if not settings.auth.enabled:
            return workspaces.LOCAL_USER, workspaces.LOCAL_USER_NAME, True
        user = sso.current_user() or {}
        return (user.get("email", ""), user.get("name", "") or user.get("email", ""), False)

    def _my_workspaces():
        if "workspace_list" not in g.__dict__:
            email, _name, anon = _identity()
            g.workspace_list = workspaces.accessible(get_store(), email, unauthenticated=anon)
        return g.workspace_list

    def _workspace():
        """The workspace this request acts in, or None when the user has none."""
        if "workspace" in g.__dict__:
            return g.workspace
        mine = _my_workspaces()
        chosen = session.get(_WORKSPACE_KEY)
        email, _name, anon = _identity()
        current = (next((w for w in mine if w.id == chosen), None)
                   or workspaces.landing(get_store(), email, unauthenticated=anon))
        if current is not None and session.get(_WORKSPACE_KEY) != current.id:
            session[_WORKSPACE_KEY] = current.id
        g.workspace = current
        return current

    def _workspace_id():
        """The scope for store queries.

        The DEFAULT workspace also claims rows that carry no workspace at all —
        anything written before workspaces existed, or by a path that forgot to
        set one. Resolving that at read time rather than rewriting every row on
        boot means the migration cannot silently lose anything, and costs no
        table scan.
        """
        return workspaces.scope_for(_workspace())

    def _in_scope(obj) -> bool:
        scope = _workspace_id()
        owner = getattr(obj, "workspace_id", None)
        if owner is None:
            return False
        return owner in scope if isinstance(scope, list) else owner == scope

    def _new_workspace_id() -> str:
        """Where a row created right now belongs — always one concrete id."""
        current = _workspace()
        return current.id if current else ""

    def _platform_app_scope() -> str | list[str]:
        """The current workspace's OAuth apps (plus its legacy unassigned apps)."""
        return workspaces.scope_for(_workspace())

    def _my_role():
        current = _workspace()
        if current is None:
            return None
        email, _name, anon = _identity()
        return workspaces.role_in(get_store(), current.id, email, unauthenticated=anon)

    def _require_owner(workspace_id=None):
        """403 unless the caller owns the workspace. Membership is not enough."""
        email, _name, anon = _identity()
        target = workspace_id or _new_workspace_id()
        if not workspaces.can_admin(get_store(), target, email, unauthenticated=anon):
            abort(403)

    def _owned(obj):
        """Return ``obj`` if it belongs to the current workspace, else 404.

        404 rather than 403 on purpose: whether an id exists in someone else's
        workspace is not this user's business.
        """
        if obj is None or not _in_scope(obj):
            abort(404)
        return obj

    @app.before_request
    def _bootstrap_workspaces():
        """Give a newly signed-in identity its workspaces, once per session."""
        if request.endpoint in sso.PUBLIC_ENDPOINTS:
            return None
        email, name, anon = _identity()
        if anon:
            return None
        if not email or session.get("workspace_bootstrapped") == email:
            return None
        workspaces.ensure_user(get_store(), email, name)
        session["workspace_bootstrapped"] = email
        return None

    @app.template_global("media_url")
    def _media_url(asset_path, download=False):
        """Where a template should point at media: blob first, us as the fallback.

        Every `<img>`/`<video>` in the dashboard goes through this, so media is
        fetched from storage directly and the VM is not in the path. Downloads
        stay on our route — a blob URL cannot set the attachment header iOS needs.
        """
        if not asset_path:
            return ""
        name = str(asset_path).rsplit("/", 1)[-1]
        if not download:
            direct = browser_url(name)
            if direct:
                return direct
        return url_for("asset", filename=name, **({"download": 1} if download else {}))

    @app.context_processor
    def _inject_workspaces():
        try:
            current = _workspace()
        except Exception:  # noqa: BLE001 - never break a page over the switcher
            return {}
        return {"current_workspace": current, "my_workspaces": _my_workspaces(),
                "my_workspace_role": _my_role()}

    @app.route("/workspaces")
    def workspaces_page():
        store = get_store()
        email, _name, anon = _identity()
        rows = []
        for workspace in _my_workspaces():
            members = store.list_members(workspace.id)
            rows.append({
                "workspace": workspace,
                "role": workspaces.role_in(store, workspace.id, email, unauthenticated=anon),
                "members": members,
                "shared": len(members) > 1,
                "counts": workspaces.content_counts(store, workspace),
            })
        return render_template("workspaces.html", rows=rows, current=_workspace())

    @app.route("/workspaces", methods=["POST"])
    def create_workspace():
        email, name, _anon = _identity()
        workspace = workspaces.create(get_store(), request.form.get("name", ""), email,
                                      display_name=name)
        session[_WORKSPACE_KEY] = workspace.id
        flash(f"Created {workspace.name}. It is private to you until you add a member.",
              "success")
        return redirect(url_for("workspaces_page"))

    @app.route("/workspaces/switch", methods=["POST"])
    def switch_workspace_form():
        """The header selector. A plain form post, so it works without JS."""
        return switch_workspace(request.form.get("workspace_id", ""))

    @app.route("/workspaces/<workspace_id>/switch", methods=["POST"])
    def switch_workspace(workspace_id):
        if not any(w.id == workspace_id for w in _my_workspaces()):
            abort(404)
        session[_WORKSPACE_KEY] = workspace_id
        # Through _safe_next: the destination comes from a form field, so it must
        # not be able to bounce anyone off-site — and it needs the reverse-proxy
        # prefix added, for the same reason the post-login redirect does.
        return redirect(sso._safe_next(request.form.get("next")))

    @app.route("/workspaces/<workspace_id>/rename", methods=["POST"])
    def rename_workspace(workspace_id):
        _require_owner(workspace_id)
        store = get_store()
        workspace = store.get_workspace(workspace_id)
        if not workspace:
            abort(404)
        workspaces.rename(store, workspace, request.form.get("name", ""))
        flash("Workspace renamed.", "success")
        return redirect(url_for("workspaces_page"))

    @app.route("/workspaces/<workspace_id>/members", methods=["POST"])
    def add_workspace_member(workspace_id):
        _require_owner(workspace_id)
        store = get_store()
        email = workspaces.normalize(request.form.get("email", ""))
        if "@" not in email:
            flash("That does not look like an email address.", "error")
            return redirect(url_for("workspaces_page"))
        role = (WorkspaceRole.owner if request.form.get("role") == "owner"
                else WorkspaceRole.member)
        store.add_member(WorkspaceMember(workspace_id=workspace_id, email=email, role=role))
        # Being a member is not the same as being allowed to sign in: the SSO
        # allowlist is a separate gate, and adding someone here does not open it.
        note = ("" if settings.auth.allows(email)
                else " They are NOT on the sign-in allowlist yet, so they cannot log in — "
                     "add them to AUTH_ALLOWED_EMAILS/AUTH_ALLOWED_DOMAINS.")
        flash(f"Added {email} as {role.value}.{note}", "success" if not note else "error")
        return redirect(url_for("workspaces_page"))

    @app.route("/workspaces/<workspace_id>/members/remove", methods=["POST"])
    def remove_workspace_member(workspace_id):
        _require_owner(workspace_id)
        store = get_store()
        email = workspaces.normalize(request.form.get("email", ""))
        remaining = [m for m in workspaces.owners(store, workspace_id) if m.email != email]
        if not remaining:
            # A workspace with no owner cannot have its membership changed ever
            # again — nobody would be able to add one back. Say what to do
            # instead, which is not the same advice in both cases: removing
            # yourself from a workspace you are alone in means deleting it.
            workspace = store.get_workspace(workspace_id)
            counts = workspaces.content_counts(store, workspace) if workspace else {}
            if len(store.list_members(workspace_id)) <= 1:
                what = ("Delete the workspace instead — that is what removing yourself from "
                        "it means." if not any(counts.values()) else
                        f"Empty it first ({counts['accounts']} account(s), "
                        f"{counts['instructions']} instruction(s), {counts['runs']} run(s), "
                        f"{counts['staged']} staged post(s)), "
                        f"then delete it.")
            else:
                what = "Make one of the other members an owner first."
            flash(f"You are the last owner of that workspace. {what}", "error")
            return redirect(url_for("workspaces_page"))
        store.remove_member(workspace_id, email)
        flash(f"Removed {email}.", "success")
        return redirect(url_for("workspaces_page"))

    @app.route("/workspaces/<workspace_id>/delete", methods=["POST"])
    def delete_workspace(workspace_id):
        _require_owner(workspace_id)
        store = get_store()
        workspace = store.get_workspace(workspace_id)
        if not workspace:
            abort(404)
        counts = workspaces.content_counts(store, workspace)
        if any(counts.values()):
            # Content is never cascaded: deleting instructions and runs by
            # accident is unrecoverable, and the accounts still hold live tokens.
            flash(f"That workspace still holds {counts['accounts']} account(s), "
                  f"{counts['instructions']} instruction(s), {counts['runs']} run(s) and "
                  f"{counts['staged']} staged post(s). "
                  f"Remove them first.", "error")
            return redirect(url_for("workspaces_page"))
        store.delete_workspace(workspace_id)
        session.pop(_WORKSPACE_KEY, None)
        flash("Workspace deleted.", "success")
        return redirect(url_for("workspaces_page"))

    # ---- overview -------------------------------------------------------- #
    @app.route("/")
    def index():
        store = get_store()
        return render_template(
            "index.html",
            accounts=store.list_accounts(workspace_id=_workspace_id()),
            instructions=store.list_instructions(workspace_id=_workspace_id()),
            pending=store.list_staged(pending_only=True, workspace_id=_workspace_id()),
            settings=settings,
            scheduler_running=scheduler.get_scheduler().running,
        )

    # ---- accounts -------------------------------------------------------- #
    @app.route("/accounts")
    def accounts():
        store = get_store()
        rows = store.list_accounts(workspace_id=_workspace_id())
        # An expired token is the difference between "publishes fine" and "401 on
        # everything tomorrow morning", so say it in words and say whether it can
        # renew itself. The refresh token is only tested for presence, never shown.
        token_state = {
            a.id: {"expiry": tokens.describe_expiry(a),
                   "stale": tokens.needs_refresh(a),
                   "refreshable": bool(store.get_tokens(a.id)[1])}
            for a in rows
        }
        return render_template("accounts.html", accounts=rows,
                               token_state=token_state, platforms=_platforms_view())

    @app.route("/accounts/<account_id>/community", methods=["POST"])
    def choose_twitter_community(account_id):
        store = get_store()
        account = _owned(store.get_account(account_id))
        _require_owner()
        if account.platform is not PlatformName.twitter:
            abort(404)
        from ..platforms.twitter import parse_community_ids

        meta = dict(account.meta)
        ids = parse_community_ids(request.form.get("community_id", ""))
        bad = [c for c in ids if not c.isdigit()]
        if bad:
            flash(f"An X Community ID contains digits only — {', '.join(bad)} does not. "
                  f"Leave the box blank for the home timeline.", "error")
            return redirect(url_for("accounts"))

        share = request.form.get("share_with_followers") == "on"
        previous = meta.get("community_ids") or ([meta["community_id"]]
                                                 if meta.get("community_id") else [])
        if ids:
            meta["community_ids"] = ids
            meta["community_id"] = ids[0]        # the single-value form, still read by older code
            meta["share_with_followers"] = share
            # Only reset the rotation when the LIST changed; re-saving the same
            # communities to flip the followers switch should not send the next
            # post back to the first one.
            if list(previous) != ids:
                meta["community_cursor"] = 0
        else:
            # No community, no choice to remember: the flag only exists to widen
            # a community post, and leaving it set would silently apply to a
            # community added later.
            for key in ("community_id", "community_ids", "share_with_followers",
                        "community_cursor"):
                meta.pop(key, None)
        account.set_meta(meta)
        store.upsert_account(account)

        if len(ids) > 1:
            flash(f"X posts rotate through {len(ids)} communities, one per run, starting with "
                  f"{ids[0]}"
                  + (" — and go to your followers too." if share
                     else ". Followers will not see them."), "success")
        elif ids:
            flash(f"X posts go to community {ids[0]}"
                  + (" and to your followers." if share
                     else " only — followers will not see them."), "success")
        else:
            flash("X posts go to the home timeline.", "success")
        return redirect(url_for("accounts"))

    @app.route("/accounts/<account_id>/check", methods=["POST"])
    def check_account(account_id):
        """What does this account's stored token ACTUALLY allow?

        The scopes AISMM asks for and the scopes a login grants are not the same
        thing — the dialog lets a user untick a Page, and a missing page
        permission stays invisible until a publish fails several minutes and one
        generated image later with a message about "impersonating a user's page".
        This asks Graph directly.
        """
        store = get_store()
        account = _owned(store.get_account(account_id))
        account_workspace = (store.get_workspace(account.workspace_id) if account.workspace_id
                             else workspaces.legacy_workspace(store))
        platform = get_platform(account.platform,
                                platform_apps.resolve_creds(account.platform, store,
                                                            (account.meta or {}).get("app_id", ""),
                                                            workspaces.scope_for(account_workspace),
                                                            allow_env=workspaces.can_use_deployment_config(
                                                                store, account_workspace)))
        # Refresh first: the diagnostic must report the token publishing would
        # actually use, not a stale one that nothing would ever send.
        access_token = tokens.valid_access_token_sync(account, store)
        if not access_token:
            flash("No stored token — reconnect this account.", "error")
            return redirect(url_for("accounts"))

        try:
            info = asyncio.run(platform.inspect_token(access_token, account))
        except Exception as exc:  # noqa: BLE001
            flash(f"Could not inspect the token: {exc}", "error")
            return redirect(url_for("accounts"))

        if not info:
            flash("Could not inspect this token — the app credentials may not match the "
                  "app that authorised it.", "error")
            return redirect(url_for("accounts"))

        granted = info.get("scopes", [])
        detail = (f"type={info.get('type') or '?'} · valid={info.get('is_valid')} · "
                  f"scopes: {', '.join(sorted(granted)) or 'not recorded'}")
        if info.get("source") == "identity check":
            # No introspection endpoint on this platform: the token was proved by
            # using it, and the scopes are the ones recorded at connect time.
            detail += " (scopes as granted at connect; "
            detail += f"{account.platform.value} has no token-introspection endpoint)"

        # The decisive check. Publishing acts as the Page, so a USER token here
        # fails with "impersonating a user's page" no matter how good the scopes
        # look — which is exactly what makes that error so confusing.
        if account.platform is PlatformName.instagram and info.get("type") == "USER":
            flash(f"This is a USER token, not a PAGE token — that is why publishing fails "
                  f"with \"impersonating a user's page\". The login did not grant access to "
                  f"the Page. Disconnect, reconnect, and on the Pages step make sure THIS "
                  f"account's Page is ticked. {detail}", "error")
            return redirect(url_for("accounts"))

        if not info.get("is_valid", True):
            why = info.get("error", "")
            flash(f"{account.platform.value} rejected this token — reconnect the account. "
                  f"{why} {detail}".strip(), "error")
            return redirect(url_for("accounts"))

        if not granted:
            # Nothing to compare against: the token works, and that is all this
            # platform will tell us. Claiming it is "healthy" would overstate it.
            flash(f"The token works — {account.platform.value} accepted it just now. "
                  f"No scope list is available for this account (connected before scopes "
                  f"were recorded, or the provider returned none). {detail}", "success")
            return redirect(url_for("accounts"))

        wanted = set(getattr(platform, "REQUIRED_SCOPES", ()) or platform.scopes)
        missing = sorted(wanted - set(granted))
        if missing:
            fix = ("Disconnect and reconnect, ticking this account's Page in the dialog."
                   if account.platform is PlatformName.instagram
                   else "Disconnect and reconnect to grant them.")
            flash(f"Token is MISSING {', '.join(missing)} — that is why publishing fails. "
                  f"{fix} {detail}", "error")
        else:
            flash(f"Looks healthy — everything publishing needs is granted. {detail}", "success")
        return redirect(url_for("accounts"))

    @app.route("/oauth/<platform>/start")
    def oauth_start(platform):
        try:
            name = PlatformName(platform)
        except ValueError:
            abort(404)
        # Connecting spends a real OAuth grant and adds a publishable account,
        # so it is an owner action, not a member one.
        _require_owner()
        app_id = request.args.get("app", "")
        creds = platform_apps.resolve_creds(
            name, get_store(), app_id, _platform_app_scope(),
            allow_env=workspaces.can_use_deployment_config(get_store(), _workspace()))
        integ = get_platform(name, creds)
        if not (integ.creds and integ.creds.configured):
            flash(f"No credentials for {platform} yet — add an app first.", "error")
            return redirect(url_for("platform_apps_page", platform=platform))
        state = oauth.random_state()
        # Remember which workspace the connect was started from: the callback
        # arrives later, and an account must never land somewhere the operator
        # was not looking when they clicked Connect.
        session[f"oauth_ws_{platform}"] = _new_workspace_id()
        session[f"oauth_state_{platform}"] = state
        # Remember which app authorised this, so the callback exchanges the code
        # with the SAME credentials and the account records its origin.
        session[f"oauth_app_{platform}"] = app_id
        code_challenge = None
        if integ.use_pkce:
            verifier, code_challenge = oauth.generate_pkce()
            session[f"oauth_verifier_{platform}"] = verifier
        url = integ.authorize_url(redirect_uri=settings.redirect_uri(platform),
                                  state=state, code_challenge=code_challenge)
        return redirect(url)

    @app.route("/oauth/<platform>/callback")
    def oauth_callback(platform):
        try:
            name = PlatformName(platform)
        except ValueError:
            abort(404)
        if request.args.get("error"):
            flash(f"{platform} authorization denied: {request.args.get('error_description', '')}", "error")
            return redirect(url_for("accounts"))
        if request.args.get("state") != session.get(f"oauth_state_{platform}"):
            flash("OAuth state mismatch — please retry the connection.", "error")
            return redirect(url_for("accounts"))
        code = request.args.get("code", "")
        app_id = session.pop(f"oauth_app_{platform}", "")
        connect_workspace = session.pop(f"oauth_ws_{platform}", "") or _new_workspace_id()
        store = get_store()
        callback_workspace = store.get_workspace(connect_workspace)
        integ = get_platform(name, platform_apps.resolve_creds(
            name, store, app_id, workspaces.scope_for(callback_workspace),
            allow_env=workspaces.can_use_deployment_config(store, callback_workspace)))
        verifier = session.get(f"oauth_verifier_{platform}")
        try:
            token = asyncio.run(integ.exchange_code(
                code=code, redirect_uri=settings.redirect_uri(platform), code_verifier=verifier))
            # EVERY profile this authorization covers, not just the first. One
            # Meta app holds a single grant per Facebook user, so claiming all the
            # linked Pages here is what stops the next connect from replacing the
            # grant the previous ones depend on.
            identities = asyncio.run(integ.fetch_identities(token.access_token))
        except Exception as exc:  # noqa: BLE001
            flash(f"Failed to connect {platform}: {exc}", "error")
            return redirect(url_for("accounts"))

        expires_at = None
        if token.expires_in:
            expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(token.expires_in))

        connected = []
        for identity in identities:
            meta = dict(identity.meta)
            if app_id:
                meta["app_id"] = app_id
            # What the provider actually GRANTED, which is not always what was
            # asked for — a consent screen can drop scopes silently. Recorded
            # here because most platforms offer no way to ask afterwards, so
            # "Check permissions" would have nothing to report otherwise.
            granted = (token.scope or "").replace(",", " ").split()
            if granted:
                meta["granted_scopes"] = granted
            # Platforms may override the token to store (Instagram: the PAGE token).
            access = meta.pop("access_token", token.access_token)
            refresh = meta.pop("refresh_token", token.refresh_token)
            acct = Account(platform=name, handle=identity.handle,
                           external_id=identity.external_id, expires_at=expires_at,
                           workspace_id=connect_workspace)
            acct.set_meta(meta)
            store.upsert_account(acct, access_token=access, refresh_token=refresh)
            connected.append((acct, identity.handle or identity.external_id))

        if not connected:
            flash(f"{platform} authorized, but no account could be stored.", "error")
            return redirect(url_for("accounts"))

        names = ", ".join(handle for _acct, handle in connected)
        flash(f"Connected {platform}: {names}"
              + (f" ({len(connected)} accounts from one login)" if len(connected) > 1 else ""),
              "success")
        _warn_about_collateral_damage(store, [a for a, _ in connected], integ,
                                      workspace_id=connect_workspace)
        return redirect(url_for("accounts"))

    @app.route("/accounts/<account_id>/delete", methods=["POST"])
    def delete_account(account_id):
        store = get_store()
        _owned(store.get_account(account_id))
        _require_owner()
        store.delete_account(account_id)
        flash("Account disconnected.", "success")
        return redirect(url_for("accounts"))

    # ---- platform apps (OAuth credentials + setup guides) ---------------- #
    @app.route("/apps")
    @app.route("/apps/<platform>")
    def platform_apps_page(platform=None):
        store = get_store()
        selected = None
        if platform:
            try:
                selected = PlatformName(platform)
            except ValueError:
                abort(404)
        return render_template(
            "apps.html",
            platforms=list(PlatformName),
            selected=selected,
            apps={p: [a for a in store.list_platform_apps(p)
                      if a.workspace_id in _platform_app_scope()] for p in PlatformName},
            guides={p.value: setup_guides.guide_for(p) for p in PlatformName},
            env_creds=({p.value: platform_apps.env_creds(p) for p in PlatformName}
                       if workspaces.can_use_deployment_config(store, _workspace()) else {}),
            env_app_id=platform_apps.ENV_APP_ID,
            redirect_uris={p.value: settings.redirect_uri(p.value) for p in PlatformName},
        )

    @app.route("/apps", methods=["POST"])
    def save_platform_app():
        _require_owner()
        store = get_store()
        f = request.form
        try:
            name = PlatformName(f.get("platform", ""))
        except ValueError:
            abort(400)
        app_id = f.get("id") or None
        record = store.get_platform_app(app_id) if app_id else None
        if record is not None and record.workspace_id not in _platform_app_scope():
            abort(404)
        if record is None:
            record = PlatformApp(platform=name, workspace_id=_new_workspace_id())
        record.platform = name
        record.name = f.get("name", "").strip()
        record.client_id = f.get("client_id", "").strip()
        record.enabled = f.get("enabled") == "on"
        extra = {key[6:]: f.get(key, "").strip()
                 for key in f if key.startswith("extra_") and f.get(key, "").strip()}
        if extra:
            record.set_extra(extra)
        # An empty secret box means "leave the stored one alone" — the form never
        # shows a secret back, so blanking it must not wipe it.
        store.upsert_platform_app(record, client_secret=f.get("client_secret", "").strip() or None)
        flash(f"Saved {name.value} app '{record.label}'.", "success")
        return redirect(url_for("platform_apps_page", platform=name.value))

    @app.route("/apps/<app_id>/delete", methods=["POST"])
    def delete_platform_app(app_id):
        _require_owner()
        store = get_store()
        record = store.get_platform_app(app_id)
        if record is None or record.workspace_id not in _platform_app_scope():
            abort(404)
        store.delete_platform_app(app_id)
        flash("App credentials deleted. Accounts connected with it keep working "
              "until their token expires.", "success")
        return redirect(url_for("platform_apps_page",
                                platform=record.platform.value if record else None))

    # ---- instructions ---------------------------------------------------- #
    INSTRUCTION_SORTS = {"name": "Name", "created_at": "Created", "schedule": "Schedule",
                         "next_run": "Next run"}

    @app.route("/instructions")
    def instructions():
        """The instruction list, searchable and sortable.

        Filtered HERE rather than in the store, unlike runs: the run table grows
        without bound and must be paged in SQL, while an operator has tens of
        instructions at most. One extra query for the thumbnails, not one per row.
        """
        store = get_store()
        scope = _workspace_id()
        rows = store.list_instructions(workspace_id=scope)
        next_runs = {i.id: _next_run_info(i, store) for i in rows}

        args = request.args
        search = args.get("q", "").strip()
        enabled = args.get("enabled", "").strip()
        mode = args.get("mode", "").strip()
        sort = args.get("sort", "name")
        if sort not in INSTRUCTION_SORTS:
            sort = "name"
        descending = args.get("dir", "asc") == "desc"

        if search:
            needle = search.lower()
            rows = [i for i in rows
                    if needle in i.name.lower() or needle in (i.brief or "").lower()
                    or needle in (i.schedule or "").lower()]
        if enabled in ("1", "0"):
            rows = [i for i in rows if i.enabled == (enabled == "1")]
        if mode:
            rows = [i for i in rows if i.publish_mode.value == mode]

        far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)

        def _next_at(instruction):
            info = next_runs.get(instruction.id) or {}
            return info.get("at") or far_future

        keys = {"name": lambda i: i.name.lower(),
                "created_at": lambda i: i.created_at,
                "schedule": lambda i: (i.schedule or "").lower(),
                "next_run": _next_at}
        rows.sort(key=keys[sort], reverse=descending)

        def instructions_url(**overrides):
            params = {"q": search, "enabled": enabled, "mode": mode, "sort": sort,
                      "dir": "desc" if descending else "asc", **overrides}
            return url_for("instructions",
                           **{k: v for k, v in params.items() if v not in ("", None)})

        return render_template(
            "instructions.html", instructions=rows, next_runs=next_runs,
            # What the schedule text was understood to MEAN. A raw cron string
            # wrapped across three lines in a narrow column is unreadable, and
            # the reading is the thing you actually want to check.
            readbacks={i.id: describe_schedule(i.schedule) if i.schedule else ""
                       for i in rows},
            total=len(rows),
            modes=list(PublishMode), sorts=INSTRUCTION_SORTS,
            filters={"q": search, "enabled": enabled, "mode": mode, "sort": sort,
                     "dir": "desc" if descending else "asc"},
            instructions_url=instructions_url,
        )

    @app.route("/instructions/new")
    def new_instruction():
        return render_template("instruction_form.html", instruction=None, state=None,
                               files=[], purposes=list(AttachmentPurpose),
                               accounts=get_store().list_accounts(workspace_id=_workspace_id()),
                               settings=settings,
                               tool_groups=_tool_catalog([]),
                               modes=list(PublishMode), media_prefs=list(MediaPref),
                               tasks=list(InstructionTask))

    @app.route("/instructions/<instruction_id>/edit")
    def edit_instruction(instruction_id):
        store = get_store()
        instr = _owned(store.get_instruction(instruction_id))
        return render_template("instruction_form.html", instruction=instr,
                               state=store.get_state(instruction_id),
                               files=store.list_instruction_files(instruction_id),
                               purposes=list(AttachmentPurpose),
                               accounts=store.list_accounts(workspace_id=_workspace_id()),
                               settings=settings,
                               schedule_readback=describe_schedule(
                                   instr.schedule, starts_at=instr.schedule_start_at),
                               next_run=_next_run_info(instr, store),
                               tool_groups=_tool_catalog(instr.tools),
                               modes=list(PublishMode), media_prefs=list(MediaPref),
                               tasks=list(InstructionTask))

    @app.route("/instructions", methods=["POST"])
    def save_instruction():
        store = get_store()
        f = request.form
        instr_id = f.get("id") or None
        instr = _owned(store.get_instruction(instr_id)) if instr_id else None
        if instr is None:
            instr = Instruction(name=f.get("name", "Untitled"),
                                workspace_id=_new_workspace_id())
        instr.name = f.get("name", "Untitled").strip() or "Untitled"
        instr.brief = f.get("brief", "").strip()
        instr.schedule = f.get("schedule", "").strip()
        instr.schedule_start_at = _parse_datetime_local(f.get("schedule_start_at", ""))
        instr.publish_mode = PublishMode(f.get("publish_mode", "dry_run"))
        instr.task_type = InstructionTask(f.get("task_type", "publish"))
        instr.media_pref = MediaPref(f.get("media_pref", "auto"))
        instr.disclose_ai = f.get("disclose_ai") == "on"
        instr.enabled = f.get("enabled") == "on"
        mine = {a.id for a in store.list_accounts(workspace_id=_workspace_id())}
        instr.set_account_ids([a for a in request.form.getlist("account_ids") if a in mine])
        # Only touch the selection when the picker was actually on the form, so a
        # POST that predates it (or omits it) leaves the stored choice alone
        # rather than resetting the instruction to every tool.
        if request.form.get("tools_present"):
            instr.set_tools(_selected_tools(request.form.getlist("tools")))
        store.upsert_instruction(instr)
        # The note is the human's channel into a running instruction; the memory
        # box is only rendered for an existing one, so leave it alone otherwise.
        store.set_note(instr.id, f.get("note", "").strip())
        if "memory" in f:
            store.set_memory(instr.id, f.get("memory", "").strip())
        _refresh_scheduler()
        flash(f"Saved instruction '{instr.name}'.", "success")

        # A file chosen on the CREATE form: there was no instruction id to hang
        # it on until now, so it rides along with the save.
        attached = False
        for upload in request.files.getlist("file"):
            if not upload or not upload.filename:
                continue
            ok, message = _attach_file(store, instr.id, upload,
                                       f.get("purpose", "context"), f.get("note", ""))
            flash(message, "success" if ok else "error")
            attached = True
        if attached:
            # Land on the edit page so they can see what was attached and add more.
            return redirect(url_for("edit_instruction", instruction_id=instr.id))
        return redirect(url_for("instructions"))

    @app.route("/instructions/<instruction_id>/delete", methods=["POST"])
    def delete_instruction(instruction_id):
        store = get_store()
        _owned(store.get_instruction(instruction_id))
        store.delete_instruction(instruction_id)
        _refresh_scheduler()
        flash("Instruction deleted.", "success")
        return redirect(url_for("instructions"))

    @app.route("/instructions/<instruction_id>/run", methods=["POST"])
    def run_instruction_now(instruction_id):
        _owned(get_store().get_instruction(instruction_id))
        # Named and logged: this thread is not the scheduler's, so when the worker
        # restarts mid-run it dies silently. The lock it holds is heartbeated, so a
        # death now frees the instruction within one lock TTL instead of blocking
        # its scheduled runs for half an hour.
        def _manual_run():
            app.logger.info("Manual run started for instruction %s", instruction_id)
            try:
                orchestrator.run_instruction(instruction_id)
            except Exception:  # noqa: BLE001 - a thread that dies quietly is undebuggable
                app.logger.exception("Manual run failed for instruction %s", instruction_id)
            finally:
                app.logger.info("Manual run finished for instruction %s", instruction_id)

        threading.Thread(target=_manual_run, name=f"manual-run:{instruction_id[:8]}",
                         daemon=True).start()
        flash("Run started — check Runs in a moment.", "success")
        return redirect(url_for("runs"))

    @app.route("/instructions/<instruction_id>/clear-cooldown", methods=["POST"])
    def clear_instruction_cooldown(instruction_id):
        """Lift the publishing cooldown on this instruction's accounts, by hand.

        An override, not a fix: the cooldown exists because the platform refused
        for volume reasons, and knocking again before it has really lifted is what
        makes Meta extend the block. The strike count is deliberately kept, so if
        the next attempt is refused too the backoff resumes where it left off
        instead of restarting at the base duration.
        """
        store = get_store()
        instruction = _owned(store.get_instruction(instruction_id))

        lifted = []
        for account_id in instruction.account_ids:
            account = store.get_account(account_id)
            if account and cooldown.clear(account, store, reset_strikes=False):
                lifted.append(account.handle or account.external_id)

        if lifted:
            app.logger.warning("Publishing cooldown cleared by hand for %s", ", ".join(lifted))
            flash(f"Cooldown cleared for {', '.join(lifted)}. If the platform is still "
                  f"blocking, the next attempt will be refused and paused for longer.",
                  "success")
        else:
            flash("No active cooldown on this instruction's accounts.", "success")
        return redirect(request.referrer or url_for("instructions"))

    # ---- instruction attachments ----------------------------------------- #
    @app.route("/instructions/<instruction_id>/files", methods=["POST"])
    def upload_instruction_file(instruction_id):
        store = get_store()
        _owned(store.get_instruction(instruction_id))
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a file to upload.", "error")
            return redirect(url_for("edit_instruction", instruction_id=instruction_id))
        ok, message = _attach_file(store, instruction_id, upload,
                                   request.form.get("purpose", "context"),
                                   request.form.get("note", ""))
        flash(message, "success" if ok else "error")
        return redirect(url_for("edit_instruction", instruction_id=instruction_id))

    @app.route("/files/<file_id>/delete", methods=["POST"])
    def delete_instruction_file(file_id):
        store = get_store()
        record = store.get_instruction_file(file_id)
        if record:
            _owned(store.get_instruction(record.instruction_id))
        store.delete_instruction_file(file_id)
        flash("Attachment removed.", "success")
        return redirect(url_for("edit_instruction",
                                instruction_id=record.instruction_id) if record
                        else url_for("instructions"))

    # ---- runs / approvals ------------------------------------------------ #
    RUN_SORTS = {"created_at": "When", "status": "Status",
                 "instruction_id": "Instruction", "account_id": "Account"}
    PER_PAGE_CHOICES = (25, 50, 100, 200)

    @app.route("/runs")
    def runs():
        store = get_store()
        args = request.args
        search = args.get("q", "").strip()
        status = args.get("status", "").strip()
        instruction_id = args.get("instruction", "").strip()
        account_id = args.get("account", "").strip()
        sort = args.get("sort", "created_at")
        if sort not in RUN_SORTS:
            sort = "created_at"
        descending = args.get("dir", "desc") != "asc"
        try:
            per_page = int(args.get("per_page", 25))
        except ValueError:
            per_page = 25
        per_page = per_page if per_page in PER_PAGE_CHOICES else 25
        try:
            page = max(int(args.get("page", 1)), 1)
        except ValueError:
            page = 1

        status_filter = None
        if status:
            try:
                status_filter = RunStatus(status)
            except ValueError:
                status_filter = None

        filters = {"status": status_filter, "instruction_id": instruction_id or None,
                   "account_id": account_id or None, "search": search,
                   "workspace_id": _workspace_id()}
        total = store.count_runs(**filters)
        pages = max((total + per_page - 1) // per_page, 1)
        page = min(page, pages)
        rows = store.list_runs(limit=per_page, offset=(page - 1) * per_page,
                               sort=sort, descending=descending, **filters)

        current = {"q": search, "status": status, "instruction": instruction_id,
                   "account": account_id, "sort": sort,
                   "dir": "desc" if descending else "asc", "per_page": per_page}

        def runs_url(**overrides):
            """A /runs URL that keeps the current filters, changing only what's given.

            Built here because Jinja macros can't take ``**kwargs``.
            """
            params = {**current, "page": page, **overrides}
            return url_for("runs", **{k: v for k, v in params.items() if v not in ("", None)})

        return render_template(
            "runs.html",
            runs=rows,
            pending=store.list_staged(pending_only=True, workspace_id=_workspace_id()),
            accounts={a.id: a for a in store.list_accounts(workspace_id=_workspace_id())},
            instructions={i.id: i for i in store.list_instructions(workspace_id=_workspace_id())},
            all_instructions=store.list_instructions(workspace_id=_workspace_id()),
            all_accounts=store.list_accounts(workspace_id=_workspace_id()),
            statuses=list(RunStatus),
            # A run stuck on "running" has no process behind it — the service was
            # restarted mid-run. Offer the tidy-up only when there is something
            # to tidy, so the button isn't permanent furniture.
            stale_runs=len(_stale_runs(store)),
            sorts=RUN_SORTS,
            per_page_choices=PER_PAGE_CHOICES,
            filters=current,
            runs_url=runs_url,
            page=page, pages=pages, total=total,
        )

    def _stale_runs(store):
        """This workspace's abandoned runs. Never another workspace's."""
        return [run for run in orchestrator.reap_stale_runs(store, apply=False)
                if _in_scope(run)]

    @app.route("/runs/reap", methods=["POST"])
    def reap_runs():
        """Close out runs abandoned by a crashed or restarted process."""
        store = get_store()
        closed = orchestrator.close_stale_runs(store, _stale_runs(store))
        flash(f"Closed {closed} abandoned run(s) — they were still marked running "
              f"long after the service that started them stopped." if closed
              else "No abandoned runs to close.", "success")
        return redirect(request.referrer or url_for("runs"))

    @app.route("/runs/<run_id>")
    def run_detail(run_id):
        store = get_store()
        run = _owned(store.get_run(run_id))
        staged = [s for s in store.list_staged(limit=500, workspace_id=_workspace_id())
                  if s.run_id == run.id]
        instruction = store.get_instruction(run.instruction_id)
        return render_template(
            "run_detail.html",
            run=run,
            system_prompt=MANAGER_INSTRUCTIONS,
            instruction_state=store.get_state(run.instruction_id) if instruction else None,
            attachments=(store.list_instruction_files(run.instruction_id)
                         if instruction else []),
            instruction=instruction,
            account=store.get_account(run.account_id),
            staged=staged,
            asset_url=public_url(run.asset_path) if run.asset_path else "",
        )

    @app.route("/runs/<run_id>/republish", methods=["POST"])
    def republish_run(run_id):
        """Send this run's existing media again — no agent, no regeneration.

        The usual reason a run failed is the publish step, not the content: a
        rate limit, an expired token, X out of API credits. Re-running the agent
        for that costs a fresh Sora clip or image and produces *different*
        content than the one already reviewed.
        """
        store = get_store()
        _owned(store.get_run(run_id))
        caption = request.form.get("caption", "")

        def _republish():
            app.logger.info("Republish of run %s started", run_id[:8])
            try:
                orchestrator.republish_run(run_id, caption)
            except Exception:  # noqa: BLE001 - a silent thread is undebuggable
                app.logger.exception("Republish of run %s failed", run_id[:8])
            finally:
                app.logger.info("Republish of run %s finished", run_id[:8])

        threading.Thread(target=_republish, name=f"republish:{run_id[:8]}",
                         daemon=True).start()
        flash("Publishing that run's media again — check Runs in a moment.", "success")
        return redirect(url_for("runs"))

    @app.route("/runs/<run_id>/retry", methods=["POST"])
    def retry_run(run_id):
        store = get_store()
        run = _owned(store.get_run(run_id))
        prompt = request.form.get("prompt", "")

        def _retry():
            app.logger.info("Retry of run %s started", run_id[:8])
            try:
                orchestrator.retry_run(run_id, prompt)
            except Exception:  # noqa: BLE001 - a thread that dies quietly is undebuggable
                app.logger.exception("Retry of run %s failed", run_id[:8])
            finally:
                app.logger.info("Retry of run %s finished", run_id[:8])

        threading.Thread(target=_retry, name=f"retry-run:{run_id[:8]}", daemon=True).start()
        flash("Retry started as a new run — check Runs in a moment.", "success")
        return redirect(url_for("runs"))

    @app.route("/staged/<staged_id>/approve", methods=["POST"])
    def approve(staged_id):
        _owned(get_store().get_staged(staged_id))
        res = orchestrator.approve_staged(staged_id)
        flash(f"Approve: {res}", "success" if res.get("status") == "published" else "error")
        # Back where it was clicked: the run page is where the post is visible.
        return redirect(request.referrer or url_for("runs"))

    @app.route("/staged/<staged_id>/reject", methods=["POST"])
    def reject(staged_id):
        _owned(get_store().get_staged(staged_id))
        orchestrator.reject_staged(staged_id)
        flash("Post rejected.", "success")
        return redirect(request.referrer or url_for("runs"))

    # ---- assets (also the PUBLIC url Instagram fetches) ------------------ #
    # Intentionally exempt from the SSO guard: Instagram fetches media from this
    # URL server-side, with no session cookie. Filenames are uuid4, so the URL
    # itself is the secret.
    @app.route("/assets/<path:filename>")
    def asset(filename):
        # ?download=1 forces a save rather than inline playback. iOS Safari gives
        # no way to save a <video> it is playing — long-press does nothing — so
        # without this there is no route to the file from a phone at all.
        as_attachment = bool(request.args.get("download"))
        local = settings.assets_dir / secure_filename(filename)
        if local.is_file():
            return send_from_directory(settings.assets_dir, filename,
                                       as_attachment=as_attachment)

        # Pruned from the local cache but still in blob storage. Streamed rather
        # than redirected so ?download=1 keeps working and a private container
        # still serves — without this, tidying the disk would break every
        # thumbnail and preview of anything older than the retention window.
        from ..store import blob_media

        if not blob_media.enabled():
            abort(404)
        try:
            data = blob_media.download(local.name)
        except Exception:  # noqa: BLE001 - genuinely gone
            abort(404)
        return send_file(io.BytesIO(data), mimetype=blob_media.content_type_for(local.name),
                         as_attachment=as_attachment, download_name=local.name)

    @app.route("/healthz")
    def healthz():
        """Unauthenticated liveness probe for the reverse proxy / systemd."""
        return {"status": "ok"}

    # ---- settings -------------------------------------------------------- #
    @app.route("/settings")
    def settings_view():
        from ..tools import sora_config
        return render_template("settings.html", settings=settings, platforms=_platforms_view(),
                               sora_enabled=sora_config.enabled(),
                               image_enabled=settings.image.enabled)

    return app


def _refresh_scheduler() -> None:
    """Re-sync scheduler jobs if the scheduler is actually running."""
    try:
        if scheduler.get_scheduler().running:
            scheduler.refresh_jobs()
    except Exception:  # noqa: BLE001
        pass


# How the tool picker groups and explains the registry, so a long flat list of
# 21 names reads as a handful of capabilities. Unknown tools fall into "Other",
# so registering a new one never breaks the page.
TOOL_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("Essentials", "Reading the brief and finishing the run.",
     ("get_context", "publish", "finish_engagement", "report_failure")),
    ("Continuity", "Carrying work across scheduled runs.",
     ("read_memory", "update_memory", "read_attachment")),
    ("Research", "Finding real, current material to post about.",
     ("web_search", "browse_page", "save_media", "describe_image")),
    ("Media", "Generating images and video.",
     ("generate_image", "generate_video", "plan_video", "create_video_sequence")),
    ("Instagram", "Reading the feed and handling comments. Ignored on other platforms.",
     ("instagram_recent_posts", "instagram_comments", "instagram_reply_to_comment",
      "instagram_moderate_comment", "instagram_insights", "instagram_publishing_limit",
      "instagram_profile", "instagram_mentions")),
    ("X (Twitter)", "Reading the timeline and replying. Ignored on other platforms; "
                    "every X call spends pay-per-use API credits.",
     ("x_recent_posts", "x_mentions", "x_replies", "x_reply_to_post", "x_post_metrics",
      "x_profile", "x_delete_post")),
    ("YouTube", "Reading and replying to comment threads. Ignored on other platforms.",
     ("youtube_comments", "youtube_reply_to_comment")),
]


def _selected_tools(posted: list[str]) -> list[str]:
    """Normalize the picker's submission for storage.

    Everything ticked is stored as an EMPTY list, which means "all" — so a tool
    added to the registry later is automatically available to instructions that
    never narrowed their selection. A genuine subset is stored verbatim.

    Nothing ticked is NOT the same as everything ticked, even though both look
    like an empty list: it is stored as the always-on tools, which is what the
    agent would get anyway. Collapsing it to "all" would mean unticking every box
    silently turned every tool back on.
    """
    from ..tools.registry import ALWAYS_ON, registered_tool_names

    available = registered_tool_names()
    chosen = [name for name in available if name in set(posted or ())]
    if len(chosen) == len(available):
        return []
    return chosen or [n for n in ALWAYS_ON if n in available]


def _tool_catalog(selected: list[str]) -> list[dict]:
    """The tool picker's model: grouped names, each with its checked state.

    An empty ``selected`` means "all", which is the default and what a brand-new
    instruction gets.
    """
    from ..tools.registry import ALWAYS_ON, registered_tool_names

    available = registered_tool_names()
    chosen = set(selected or available)
    grouped, placed = [], set()
    for title, blurb, names in TOOL_GROUPS:
        rows = [{"name": n, "checked": n in chosen, "always_on": n in ALWAYS_ON}
                for n in names if n in available]
        placed.update(r["name"] for r in rows)
        if rows:
            grouped.append({"title": title, "blurb": blurb, "tools": rows})

    leftover = [{"name": n, "checked": n in chosen, "always_on": n in ALWAYS_ON}
                for n in available if n not in placed]
    if leftover:
        grouped.append({"title": "Other", "blurb": "", "tools": leftover})
    return grouped


MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _attach_file(store, instruction_id: str, upload, purpose_value: str, note: str):
    """Store one uploaded file against an instruction. Returns ``(ok, message)``.

    Shared by the create form and the edit page's uploader so a file attached
    while creating an instruction goes through exactly the same extraction,
    size limit and purpose handling — the create form used to have no uploader
    at all, because an attachment needs an instruction id and there is none
    until the row is saved. It is saved first, then the file is attached.
    """
    data = upload.read()
    if not data:
        return False, f"{upload.filename} is empty."
    if len(data) > MAX_UPLOAD_BYTES:
        return False, (f"{upload.filename} is {len(data) // 1024 // 1024}MB; the limit is "
                       f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB.")

    filename = secure_filename(upload.filename) or "upload"
    suffix = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    content_type = upload.mimetype or ""
    try:
        purpose = AttachmentPurpose(purpose_value or "context")
    except ValueError:
        purpose = AttachmentPurpose.context

    text, extraction_note = attachments.extract_text(data, content_type, filename)
    store.add_instruction_file(InstructionFile(
        instruction_id=instruction_id, filename=filename, content_type=content_type,
        purpose=purpose, asset_path=save_bytes(data, suffix), size_bytes=len(data),
        text=text, note=(note or "").strip() or extraction_note))
    return True, (f"Attached {filename}"
                  + (f" — {len(text):,} characters of text extracted" if text
                     else f" ({extraction_note})" if extraction_note else ""))


def _warn_about_collateral_damage(store, just_connected, platform,
                                  workspace_id: str | None = None) -> None:
    """Did connecting THIS account break the ones already connected?

    One Meta app + one Facebook user = ONE grant. Authorising again replaces it
    wholesale, so connecting a second Instagram account with only its own Page
    ticked strips ``pages_show_list`` / ``pages_read_engagement`` from the grant
    that the *earlier* accounts' page tokens were minted against. They keep
    looking connected and fail hours later with

        … must be granted before impersonating a user's page [code=190]

    on whatever run happens to come next. Checking here turns that into a warning
    on the page you are already looking at, while the dialog is still fresh in
    mind. Best-effort: never blocks or undoes the connection that just succeeded.
    """
    inspect = getattr(platform, "inspect_token", None)
    if inspect is None:
        return

    fresh = just_connected if isinstance(just_connected, list) else [just_connected]
    fresh_ids = {a.id for a in fresh}
    platform_name = fresh[0].platform

    broken = []
    for other in store.list_accounts(workspace_id=workspace_id):
        if other.platform is not platform_name or other.id in fresh_ids:
            continue
        try:
            token, _ = store.get_tokens(other.id)
            info = asyncio.run(inspect(token)) if token else {}
        except Exception as exc:  # noqa: BLE001 - a warning must never break a connect
            logging.getLogger("aismm.dashboard").warning(
                "Could not re-check %s after connecting: %s", other.handle, exc)
            continue
        if not info:
            continue
        required = set(getattr(platform, "REQUIRED_SCOPES", ()) or ())
        if info.get("type") == "USER" or not info.get("is_valid", True) or (
                required and required - set(info.get("scopes", []))):
            broken.append(other.handle or other.external_id)

    if broken:
        flash(f"⚠️ Connecting this account appears to have REVOKED page access for "
              f"{', '.join(broken)} — one Meta app can hold only one grant per Facebook "
              f"user, and authorising again replaces the previous Page selection. Reconnect "
              f"{'them' if len(broken) > 1 else 'it'} and tick EVERY Page you publish to, "
              f"not just the one you are adding.", "error")


def _next_run_info(instruction, store) -> dict:
    """When this instruction next fires, and whether that fire will do anything.

    The scheduler's next fire time is not the same as the next POST: the
    orchestrator skips a ``live`` run whose account is in a publishing cooldown
    (``_run_one``), so showing the raw fire time promised a run that would be
    logged as "Skipping … rate-limited" a minute later. Mirrors that same rule —
    only ``live`` mode is affected, and only when EVERY target account is blocked,
    since one free account still makes the fire worth firing.
    """
    fires_at = scheduler.next_run_for(instruction.id)
    accounts = [a for a in (store.get_account(i) for i in instruction.account_ids) if a]
    # Listed whatever the publish mode, so the operator can always see (and lift)
    # a cooldown — a dry_run instruction still shares its account with live ones.
    info = {"at": fires_at, "blocked_until": None, "skipped": False,
            "cooling": [{"handle": a.handle or a.external_id, "for": cooldown.describe(a)}
                        for a in accounts if cooldown.is_active(a)]}
    if not fires_at or instruction.publish_mode is not PublishMode.live or not accounts:
        return info

    deadlines = [cooldown.deadline(a) for a in accounts]
    if not all(deadlines):
        return info                      # at least one account can still publish

    blocked_until = max(deadlines)
    if fires_at > blocked_until:
        return info                      # the cooldown clears before that fire

    info["skipped"] = True
    info["blocked_until"] = blocked_until
    info["at"] = scheduler.next_run_after(instruction.id, blocked_until) or fires_at
    return info


def _parse_datetime_local(value: str) -> dt.datetime | None:
    """Parse an ``<input type="datetime-local">`` value as UTC.

    The value has no timezone ("2026-08-05T09:00") — schedules are documented as
    UTC throughout, so it's read as UTC rather than the browser's local zone. An
    empty or unparseable value clears the field (interval schedules then anchor to
    ``created_at`` instead — see aismm/schedules.py).
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed
