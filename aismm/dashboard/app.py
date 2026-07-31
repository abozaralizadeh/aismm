"""The AISMM dashboard (Flask).

This is the control center: connect social accounts via OAuth, author Instructions
(select accounts + brief + schedule + publish mode), and review/approve posts.
Async platform/agent calls are driven from sync routes via ``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import threading
from collections.abc import Callable

from flask import (
    Flask, abort, flash, redirect, render_template, request, send_from_directory, session, url_for,
)
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename

from ..config import settings
from ..assets import public_url
from .. import attachments, cooldown
from ..agent.prompts import MANAGER_INSTRUCTIONS
from ..assets import save_bytes
from ..models import (
    Account, AttachmentPurpose, Instruction, InstructionFile, MediaPref, PlatformApp,
    PlatformName, PublishMode, RunStatus,
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
        view = []
        for p in PlatformName:
            view.append({"name": p.value,
                         "configured": platform_apps.is_configured(p, store),
                         "options": platform_apps.connection_options(p, store),
                         "capabilities": get_platform(p).capabilities})
        return view

    # ---- overview -------------------------------------------------------- #
    @app.route("/")
    def index():
        store = get_store()
        return render_template(
            "index.html",
            accounts=store.list_accounts(),
            instructions=store.list_instructions(),
            pending=store.list_staged(pending_only=True),
            settings=settings,
            scheduler_running=scheduler.get_scheduler().running,
        )

    # ---- accounts -------------------------------------------------------- #
    @app.route("/accounts")
    def accounts():
        return render_template("accounts.html", accounts=get_store().list_accounts(),
                               platforms=_platforms_view())

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
        account = store.get_account(account_id)
        if not account:
            abort(404)
        platform = get_platform(account.platform,
                                platform_apps.resolve_creds(account.platform, store,
                                                            (account.meta or {}).get("app_id", "")))
        access_token, _ = store.get_tokens(account.id)
        if not access_token:
            flash("No stored token — reconnect this account.", "error")
            return redirect(url_for("accounts"))

        try:
            info = asyncio.run(platform.inspect_token(access_token))
        except Exception as exc:  # noqa: BLE001
            flash(f"Could not inspect the token: {exc}", "error")
            return redirect(url_for("accounts"))

        if not info:
            flash("Could not inspect this token — the app credentials may not match the "
                  "app that authorised it.", "error")
            return redirect(url_for("accounts"))

        granted = info.get("scopes", [])
        detail = (f"type={info.get('type') or '?'} · valid={info.get('is_valid')} · "
                  f"scopes: {', '.join(sorted(granted)) or 'none'}")

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
            flash(f"Instagram says this token is no longer valid — reconnect. {detail}", "error")
            return redirect(url_for("accounts"))

        wanted = set(getattr(platform, "REQUIRED_SCOPES", ()) or platform.scopes)
        missing = sorted(wanted - set(granted))
        if missing:
            flash(f"Token is MISSING {', '.join(missing)} — that is why publishing fails. "
                  f"Disconnect and reconnect, ticking this account's Page in the dialog. "
                  f"{detail}", "error")
        else:
            flash(f"Looks healthy — everything publishing needs is granted. {detail}", "success")
        return redirect(url_for("accounts"))

    @app.route("/oauth/<platform>/start")
    def oauth_start(platform):
        try:
            name = PlatformName(platform)
        except ValueError:
            abort(404)
        app_id = request.args.get("app", "")
        creds = platform_apps.resolve_creds(name, get_store(), app_id)
        integ = get_platform(name, creds)
        if not (integ.creds and integ.creds.configured):
            flash(f"No credentials for {platform} yet — add an app first.", "error")
            return redirect(url_for("platform_apps_page", platform=platform))
        state = oauth.random_state()
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
        integ = get_platform(name, platform_apps.resolve_creds(name, get_store(), app_id))
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

        store = get_store()
        connected = []
        for identity in identities:
            meta = dict(identity.meta)
            if app_id:
                meta["app_id"] = app_id
            # Platforms may override the token to store (Instagram: the PAGE token).
            access = meta.pop("access_token", token.access_token)
            refresh = meta.pop("refresh_token", token.refresh_token)
            acct = Account(platform=name, handle=identity.handle,
                           external_id=identity.external_id, expires_at=expires_at)
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
        _warn_about_collateral_damage(store, [a for a, _ in connected], integ)
        return redirect(url_for("accounts"))

    @app.route("/accounts/<account_id>/delete", methods=["POST"])
    def delete_account(account_id):
        get_store().delete_account(account_id)
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
            apps={p: store.list_platform_apps(p) for p in PlatformName},
            guides={p.value: setup_guides.guide_for(p) for p in PlatformName},
            env_creds={p.value: platform_apps.env_creds(p) for p in PlatformName},
            env_app_id=platform_apps.ENV_APP_ID,
            redirect_uris={p.value: settings.redirect_uri(p.value) for p in PlatformName},
        )

    @app.route("/apps", methods=["POST"])
    def save_platform_app():
        store = get_store()
        f = request.form
        try:
            name = PlatformName(f.get("platform", ""))
        except ValueError:
            abort(400)
        app_id = f.get("id") or None
        record = store.get_platform_app(app_id) if app_id else None
        if record is None:
            record = PlatformApp(platform=name)
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
        store = get_store()
        record = store.get_platform_app(app_id)
        store.delete_platform_app(app_id)
        flash("App credentials deleted. Accounts connected with it keep working "
              "until their token expires.", "success")
        return redirect(url_for("platform_apps_page",
                                platform=record.platform.value if record else None))

    # ---- instructions ---------------------------------------------------- #
    @app.route("/instructions")
    def instructions():
        store = get_store()
        instrs = store.list_instructions()
        next_runs = {i.id: _next_run_info(i, store) for i in instrs}
        return render_template("instructions.html", instructions=instrs, next_runs=next_runs)

    @app.route("/instructions/new")
    def new_instruction():
        return render_template("instruction_form.html", instruction=None, state=None,
                               files=[], purposes=list(AttachmentPurpose),
                               accounts=get_store().list_accounts(), settings=settings,
                               tool_groups=_tool_catalog([]),
                               modes=list(PublishMode), media_prefs=list(MediaPref))

    @app.route("/instructions/<instruction_id>/edit")
    def edit_instruction(instruction_id):
        store = get_store()
        instr = store.get_instruction(instruction_id)
        if not instr:
            abort(404)
        return render_template("instruction_form.html", instruction=instr,
                               state=store.get_state(instruction_id),
                               files=store.list_instruction_files(instruction_id),
                               purposes=list(AttachmentPurpose),
                               accounts=store.list_accounts(), settings=settings,
                               schedule_readback=describe_schedule(
                                   instr.schedule, starts_at=instr.schedule_start_at),
                               next_run=_next_run_info(instr, store),
                               tool_groups=_tool_catalog(instr.tools),
                               modes=list(PublishMode), media_prefs=list(MediaPref))

    @app.route("/instructions", methods=["POST"])
    def save_instruction():
        store = get_store()
        f = request.form
        instr_id = f.get("id") or None
        instr = store.get_instruction(instr_id) if instr_id else None
        if instr is None:
            instr = Instruction(name=f.get("name", "Untitled"))
        instr.name = f.get("name", "Untitled").strip() or "Untitled"
        instr.brief = f.get("brief", "").strip()
        instr.schedule = f.get("schedule", "").strip()
        instr.schedule_start_at = _parse_datetime_local(f.get("schedule_start_at", ""))
        instr.publish_mode = PublishMode(f.get("publish_mode", "dry_run"))
        instr.media_pref = MediaPref(f.get("media_pref", "auto"))
        instr.disclose_ai = f.get("disclose_ai") == "on"
        instr.enabled = f.get("enabled") == "on"
        instr.set_account_ids(request.form.getlist("account_ids"))
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
        return redirect(url_for("instructions"))

    @app.route("/instructions/<instruction_id>/delete", methods=["POST"])
    def delete_instruction(instruction_id):
        get_store().delete_instruction(instruction_id)
        _refresh_scheduler()
        flash("Instruction deleted.", "success")
        return redirect(url_for("instructions"))

    @app.route("/instructions/<instruction_id>/run", methods=["POST"])
    def run_instruction_now(instruction_id):
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
        instruction = store.get_instruction(instruction_id)
        if not instruction:
            abort(404)

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
    MAX_UPLOAD_BYTES = 25 * 1024 * 1024

    @app.route("/instructions/<instruction_id>/files", methods=["POST"])
    def upload_instruction_file(instruction_id):
        store = get_store()
        if not store.get_instruction(instruction_id):
            abort(404)
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a file to upload.", "error")
            return redirect(url_for("edit_instruction", instruction_id=instruction_id))

        data = upload.read()
        if len(data) > MAX_UPLOAD_BYTES:
            flash(f"{upload.filename} is {len(data) // 1024 // 1024}MB; the limit is "
                  f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB.", "error")
            return redirect(url_for("edit_instruction", instruction_id=instruction_id))

        filename = secure_filename(upload.filename) or "upload"
        suffix = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        content_type = upload.mimetype or ""
        try:
            purpose = AttachmentPurpose(request.form.get("purpose", "context"))
        except ValueError:
            purpose = AttachmentPurpose.context

        text, note = attachments.extract_text(data, content_type, filename)
        record = InstructionFile(
            instruction_id=instruction_id, filename=filename, content_type=content_type,
            purpose=purpose, asset_path=save_bytes(data, suffix), size_bytes=len(data),
            text=text, note=request.form.get("note", "").strip() or note)
        store.add_instruction_file(record)
        flash(f"Attached {filename}" + (f" — {len(text):,} characters of text extracted"
                                        if text else f" ({note})" if note else ""), "success")
        return redirect(url_for("edit_instruction", instruction_id=instruction_id))

    @app.route("/files/<file_id>/delete", methods=["POST"])
    def delete_instruction_file(file_id):
        store = get_store()
        record = store.get_instruction_file(file_id)
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
                   "account_id": account_id or None, "search": search}
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
            pending=store.list_staged(pending_only=True),
            accounts={a.id: a for a in store.list_accounts()},
            instructions={i.id: i for i in store.list_instructions()},
            all_instructions=store.list_instructions(),
            all_accounts=store.list_accounts(),
            statuses=list(RunStatus),
            sorts=RUN_SORTS,
            per_page_choices=PER_PAGE_CHOICES,
            filters=current,
            runs_url=runs_url,
            page=page, pages=pages, total=total,
        )

    @app.route("/runs/<run_id>")
    def run_detail(run_id):
        store = get_store()
        run = store.get_run(run_id)
        if not run:
            abort(404)
        staged = [s for s in store.list_staged(limit=500) if s.run_id == run.id]
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

    @app.route("/runs/<run_id>/retry", methods=["POST"])
    def retry_run(run_id):
        store = get_store()
        run = store.get_run(run_id)
        if not run:
            abort(404)
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
        res = orchestrator.approve_staged(staged_id)
        flash(f"Approve: {res}", "success" if res.get("status") == "published" else "error")
        return redirect(url_for("runs"))

    @app.route("/staged/<staged_id>/reject", methods=["POST"])
    def reject(staged_id):
        orchestrator.reject_staged(staged_id)
        flash("Post rejected.", "success")
        return redirect(url_for("runs"))

    # ---- assets (also the PUBLIC url Instagram fetches) ------------------ #
    # Intentionally exempt from the SSO guard: Instagram fetches media from this
    # URL server-side, with no session cookie. Filenames are uuid4, so the URL
    # itself is the secret.
    @app.route("/assets/<path:filename>")
    def asset(filename):
        return send_from_directory(settings.assets_dir, filename)

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
     ("get_context", "publish", "report_failure")),
    ("Continuity", "Carrying work across scheduled runs.",
     ("read_memory", "update_memory", "read_attachment")),
    ("Research", "Finding real, current material to post about.",
     ("web_search", "browse_page", "save_media")),
    ("Media", "Generating images and video.",
     ("generate_image", "generate_video", "plan_video", "create_video_sequence")),
    ("Instagram", "Reading the feed and handling comments. Ignored on other platforms.",
     ("instagram_recent_posts", "instagram_comments", "instagram_reply_to_comment",
      "instagram_moderate_comment", "instagram_insights", "instagram_publishing_limit",
      "instagram_profile", "instagram_mentions")),
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


def _warn_about_collateral_damage(store, just_connected, platform) -> None:
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
    for other in store.list_accounts():
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
