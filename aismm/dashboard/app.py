"""The AISMM dashboard (Flask).

This is the control center: connect social accounts via OAuth, author Instructions
(select accounts + brief + schedule + publish mode), and review/approve posts.
Async platform/agent calls are driven from sync routes via ``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
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
from .. import attachments
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
            identity = asyncio.run(integ.fetch_identity(token.access_token))
        except Exception as exc:  # noqa: BLE001
            flash(f"Failed to connect {platform}: {exc}", "error")
            return redirect(url_for("accounts"))

        meta = dict(identity.meta)
        if app_id:
            meta["app_id"] = app_id
        access = meta.pop("access_token", token.access_token)   # platforms may override (e.g. IG page token)
        refresh = meta.pop("refresh_token", token.refresh_token)
        expires_at = None
        if token.expires_in:
            expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(token.expires_in))
        acct = Account(platform=name, handle=identity.handle, external_id=identity.external_id,
                       expires_at=expires_at)
        acct.set_meta(meta)
        get_store().upsert_account(acct, access_token=access, refresh_token=refresh)
        flash(f"Connected {platform}: {identity.handle or identity.external_id}", "success")
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
        return render_template("instructions.html", instructions=get_store().list_instructions())

    @app.route("/instructions/new")
    def new_instruction():
        return render_template("instruction_form.html", instruction=None, state=None,
                               files=[], purposes=list(AttachmentPurpose),
                               accounts=get_store().list_accounts(), settings=settings,
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
                               schedule_readback=describe_schedule(instr.schedule),
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
        instr.publish_mode = PublishMode(f.get("publish_mode", "dry_run"))
        instr.media_pref = MediaPref(f.get("media_pref", "auto"))
        instr.disclose_ai = f.get("disclose_ai") == "on"
        instr.enabled = f.get("enabled") == "on"
        instr.set_account_ids(request.form.getlist("account_ids"))
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
        threading.Thread(target=orchestrator.run_instruction, args=(instruction_id,),
                         daemon=True).start()
        flash("Run started — check Runs in a moment.", "success")
        return redirect(url_for("runs"))

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
