"""The AISMM dashboard (Flask).

This is the control center: connect social accounts via OAuth, author Instructions
(select accounts + brief + schedule + publish mode), and review/approve posts.
Async platform/agent calls are driven from sync routes via ``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import threading

from flask import (
    Flask, abort, flash, redirect, render_template, request, send_from_directory, session, url_for,
)

from ..config import settings
from ..models import Account, Instruction, MediaPref, PlatformName, PublishMode
from ..platforms.registry import get_platform
from ..auth import oauth
from .. import orchestrator, scheduler
from ..store import get_store


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = settings.dashboard.secret_key

    # ---- helpers --------------------------------------------------------- #
    def _platforms_view():
        view = []
        for p in PlatformName:
            creds = settings.platform_creds.get(p.value)
            view.append({"name": p.value,
                         "configured": bool(creds and creds.configured),
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
        integ = get_platform(name)
        if not (integ.creds and integ.creds.configured):
            flash(f"{platform} app credentials are not set. Add them to your .env.", "error")
            return redirect(url_for("accounts"))
        state = oauth.random_state()
        session[f"oauth_state_{platform}"] = state
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
        integ = get_platform(name)
        verifier = session.get(f"oauth_verifier_{platform}")
        try:
            token = asyncio.run(integ.exchange_code(
                code=code, redirect_uri=settings.redirect_uri(platform), code_verifier=verifier))
            identity = asyncio.run(integ.fetch_identity(token.access_token))
        except Exception as exc:  # noqa: BLE001
            flash(f"Failed to connect {platform}: {exc}", "error")
            return redirect(url_for("accounts"))

        meta = dict(identity.meta)
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

    # ---- instructions ---------------------------------------------------- #
    @app.route("/instructions")
    def instructions():
        return render_template("instructions.html", instructions=get_store().list_instructions())

    @app.route("/instructions/new")
    def new_instruction():
        return render_template("instruction_form.html", instruction=None,
                               accounts=get_store().list_accounts(),
                               modes=list(PublishMode), media_prefs=list(MediaPref))

    @app.route("/instructions/<instruction_id>/edit")
    def edit_instruction(instruction_id):
        instr = get_store().get_instruction(instruction_id)
        if not instr:
            abort(404)
        return render_template("instruction_form.html", instruction=instr,
                               accounts=get_store().list_accounts(),
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
        instr.enabled = f.get("enabled") == "on"
        instr.set_account_ids(request.form.getlist("account_ids"))
        store.upsert_instruction(instr)
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

    # ---- runs / approvals ------------------------------------------------ #
    @app.route("/runs")
    def runs():
        store = get_store()
        return render_template("runs.html", runs=store.list_runs(limit=100),
                               pending=store.list_staged(pending_only=True),
                               accounts={a.id: a for a in store.list_accounts()})

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
    @app.route("/assets/<path:filename>")
    def asset(filename):
        return send_from_directory(settings.assets_dir, filename)

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
