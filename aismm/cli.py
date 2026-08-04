"""Command-line interface: ``aismm <command>``.

    aismm run          start the scheduler AND the dashboard (default)
    aismm dashboard    start only the dashboard
    aismm scheduler    start only the scheduler (headless)
    aismm auth <p>     print the connect URL for a platform (open in a browser)
    aismm list         list connected accounts and instructions
    aismm post ...     run one instruction once, now (honors its publish mode)
"""
from __future__ import annotations

import argparse
import sys
import time

from .config import ensure_dirs, settings
from .logging_setup import configure_logging


def cmd_run(_args) -> int:
    from . import scheduler
    from .dashboard import create_app
    from .llm import configure_tracing

    configure_logging()
    ensure_dirs()
    configure_tracing()
    scheduler.start()
    app = create_app()
    print(f"AISMM dashboard → {settings.dashboard.public_base_url}  (scheduler running)")
    app.run(host=settings.dashboard.host, port=settings.dashboard.port,
            threaded=True, use_reloader=False)
    return 0


def cmd_dashboard(_args) -> int:
    from .dashboard import create_app
    from .llm import configure_tracing

    configure_logging()
    ensure_dirs()
    configure_tracing()   # "Run now" drives the agent from here too
    app = create_app()
    print(f"AISMM dashboard → {settings.dashboard.public_base_url}  (no scheduler)")
    app.run(host=settings.dashboard.host, port=settings.dashboard.port,
            threaded=True, use_reloader=False)
    return 0


def cmd_scheduler(_args) -> int:
    from . import scheduler
    from .llm import configure_tracing

    configure_logging()
    ensure_dirs()
    configure_tracing()
    scheduler.start()
    print("AISMM scheduler running. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        return 0


def cmd_auth(args) -> int:
    from .models import PlatformName
    from .platforms.registry import get_platform
    from .auth import oauth

    try:
        name = PlatformName(args.platform)
    except ValueError:
        print(f"Unknown platform '{args.platform}'. Choose: "
              + ", ".join(p.value for p in PlatformName))
        return 1
    integ = get_platform(name)
    if not (integ.creds and integ.creds.configured):
        print(f"{args.platform} app credentials are missing. Add them to your .env first.")
        return 1
    print("Connecting via the dashboard is recommended (it handles the OAuth callback).")
    start_url = settings.dashboard.external_url(f"oauth/{args.platform}/start")
    print(f"Start the dashboard, then open:\n  {start_url}\n")
    challenge = oauth.generate_pkce()[1] if integ.use_pkce else None
    url = integ.authorize_url(redirect_uri=settings.redirect_uri(args.platform),
                              state=oauth.random_state(), code_challenge=challenge)
    print(f"Direct authorize URL (callback must reach {settings.redirect_uri(args.platform)}):\n  {url}")
    return 0


def cmd_list(_args) -> int:
    from .store import get_store

    store = get_store()
    accounts = store.list_accounts()
    print(f"\nAccounts ({len(accounts)}):")
    for a in accounts:
        print(f"  {a.id[:8]}  {a.platform.value:10}  {a.handle or a.external_id}")
    instrs = store.list_instructions()
    print(f"\nInstructions ({len(instrs)}):")
    for i in instrs:
        print(f"  {i.id[:8]}  {i.name:24}  mode={i.publish_mode.value:8}  "
              f"sched={i.schedule or '—':12}  accounts={len(i.account_ids)}  "
              f"{'on' if i.enabled else 'off'}")
    print()
    return 0


def cmd_workspaces(args) -> int:
    """Show who can see what, and optionally tidy up unassigned rows.

    Unassigned rows are already visible in the default workspace (its scope
    matches them at read time), so --adopt is housekeeping, not a repair.
    """
    from . import workspaces
    from .store import get_store

    configure_logging()
    store = get_store()
    default = workspaces.ensure_default(store)
    for workspace in store.list_workspaces():
        counts = workspaces.content_counts(store, workspace.id)
        flags = []
        if workspace.id == default.id:
            flags.append("default")
        if workspace.auto_join:
            flags.append("everyone joins")
        print(f"\n{workspace.name}  [{workspace.kind.value}"
              f"{', ' + ', '.join(flags) if flags else ''}]")
        print(f"  id={workspace.id}")
        print(f"  {counts['accounts']} account(s), {counts['instructions']} instruction(s), "
              f"{counts['runs']} run(s)")
        for member in store.list_members(workspace.id):
            print(f"    {member.role.value:6}  {member.email}")
    if args.adopt:
        moved = workspaces.adopt_orphans(store, default.id)
        print(f"\nAssigned {moved} previously unassigned row(s) to '{default.name}'.")
    print()
    return 0


def cmd_post(args) -> int:
    from .store import get_store
    from .llm import configure_tracing
    from . import orchestrator

    configure_logging()
    ensure_dirs()
    configure_tracing()
    store = get_store()
    instr = store.get_instruction(args.instruction)
    if not instr:
        # allow prefix match on id or exact name
        matches = [i for i in store.list_instructions()
                   if i.id.startswith(args.instruction) or i.name == args.instruction]
        instr = matches[0] if len(matches) == 1 else None
    if not instr:
        print(f"Instruction '{args.instruction}' not found (use `aismm list`).")
        return 1

    if args.account:
        acct = store.get_account(args.account)
        if not acct:
            print(f"Account '{args.account}' not found.")
            return 1
        print(orchestrator.run_single(instr, acct))
    else:
        results = orchestrator.run_instruction(instr.id)
        for r in results:
            print(r)
    return 0


def cmd_reconcile(args) -> int:
    """Repair runs recorded as failed whose post is actually live on the account."""
    from . import reconcile
    from .store import get_store

    configure_logging()
    store = get_store()
    reports = reconcile.reconcile_all(store, apply=args.apply)
    if not reports:
        print("No Instagram accounts to reconcile.")
        return 0

    total = 0
    for report in reports:
        if report.get("error"):
            print(f"\n{report['account']}: ERROR {report['error']}")
            continue
        found = report["repaired"]
        total += len(found)
        print(f"\n{report['account']}: {report['live_posts']} live post(s), "
              f"{report['failed_runs']} failed run(s) checked")
        for run_id, url in found:
            print(f"  run {run_id[:8]} IS published -> {url or '(no permalink)'}")
        if found:
            print(f"  {'repaired' if report['applied'] else 'would repair'}: {len(found)}"
                  + (f", ledger entries added: {report['ledger_seeded']}"
                     if report["applied"] else ""))
        else:
            print("  nothing to repair")

    if total and not args.apply:
        print("\nThis was a dry run. Re-run with --apply to write the changes.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aismm", description="AI Social Media Manager")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("run", help="start scheduler + dashboard").set_defaults(func=cmd_run)
    sub.add_parser("dashboard", help="start dashboard only").set_defaults(func=cmd_dashboard)
    sub.add_parser("scheduler", help="start scheduler only").set_defaults(func=cmd_scheduler)

    pa = sub.add_parser("auth", help="print a platform connect URL")
    pa.add_argument("platform")
    pa.set_defaults(func=cmd_auth)

    sub.add_parser("list", help="list accounts and instructions").set_defaults(func=cmd_list)

    pp = sub.add_parser("post", help="run one instruction once, now")
    pp.add_argument("--instruction", required=True, help="instruction id (prefix) or name")
    pp.add_argument("--account", help="limit to one account id")
    pp.set_defaults(func=cmd_post)

    pw = sub.add_parser("workspaces", help="list workspaces, members and their content")
    pw.add_argument("--adopt", action="store_true",
                    help="write a workspace onto rows that predate workspaces (tidy-up only)")
    pw.set_defaults(func=cmd_workspaces)

    pr = sub.add_parser("reconcile",
                        help="fix runs marked failed whose post is actually live")
    pr.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    pr.set_defaults(func=cmd_reconcile)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # default to `run`
        return cmd_run(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
