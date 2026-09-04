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


def cmd_assets(args) -> int:
    """Show what the local asset cache is holding, and optionally free it."""
    from . import assets, orchestrator
    from .store import blob_media, get_store

    configure_logging()
    usage = assets.local_usage()
    print(f"\nLocal asset cache: {usage['files']:,} file(s), "
          f"{usage['bytes'] / 1e6:,.1f} MB, oldest {usage['oldest_days']} days")
    print(f"Blob storage:      {'configured' if blob_media.enabled() else 'NOT configured'}")
    if not blob_media.enabled():
        print("\nWithout blob storage the local files are the only copy, so nothing is "
              "pruned. Set AZURE_STORAGE_CONNECTION_STRING first.\n")
        return 0

    result = orchestrator.prune_asset_cache(get_store(), older_than_days=args.older_than,
                                            apply=args.apply)
    verb = "Deleted" if args.apply else "Would delete"
    print(f"\n{verb} {result['deleted']:,} file(s), freeing {result['freed_bytes'] / 1e6:,.1f} MB")
    print(f"  kept (too recent):        {result['skipped_recent']:,}")
    print(f"  kept (NOT in blob yet):   {result['kept_local_only']:,}")
    if result["kept_local_only"]:
        print("  ^ those are the only copy — check that blob uploads are working.")
    if not args.apply:
        print("\nRe-run with --apply to delete them.")
    print()
    return 0


def cmd_runs(args) -> int:
    """Close out runs left marked ``running`` by a process that died."""
    from . import orchestrator
    from .store import get_store

    configure_logging()
    store = get_store()
    stale = orchestrator.reap_stale_runs(store, older_than_seconds=args.older_than,
                                         apply=args.apply)
    cutoff = orchestrator.stale_run_cutoff(args.older_than)
    if not stale:
        print(f"\nNo runs have been stuck on 'running' for more than "
              f"{cutoff // 60} minutes.")
        return 0
    print(f"\n{'Marked failed' if args.apply else 'Would mark failed'} "
          f"({len(stale)}, stuck for more than {cutoff // 60} minutes):")
    for run in stale:
        print(f"  {run.id[:8]}  started {run.created_at:%Y-%m-%d %H:%M}  "
              f"instruction={run.instruction_id[:8]}")
    if not args.apply:
        print("\nRe-run with --apply to write the change.")
    print()
    return 0


def cmd_workspaces(args) -> int:
    """Show who can see what; take ownership; optionally tidy up unassigned rows.

    ``--owner`` is the repair for a workspace with no owner — an earlier build
    created its migration workspace that way, and with no owner its membership
    could never be changed by anyone, so nobody could invite anyone or hand it
    over. Signing in also repairs it now; this is the direct route.

    ``--adopt`` is housekeeping, not a repair: unassigned rows are already
    visible in the workspace that claims them, at read time.
    """
    from . import workspaces
    from .store import get_store

    configure_logging()
    store = get_store()
    legacy = workspaces.legacy_workspace(store)

    if args.assign_app:
        app = store.get_platform_app(args.assign_app)
        target = workspaces.find(store, args.workspace)
        if app is None:
            print(f"No OAuth app matches {args.assign_app!r}.")
            return 1
        if target is None:
            print("Pass --workspace <workspace id or name> with --assign-app.")
            return 1
        app.workspace_id = target.id
        store.upsert_platform_app(app)
        print(f"Assigned OAuth app '{app.label}' ({app.id}) to '{target.name}'.")

    if args.owner:
        target = workspaces.find(store, args.workspace) if args.workspace else legacy
        if target is None:
            print("Could not decide which workspace to act on. Pass --workspace <id or name>; "
                  "the ids are listed below." if not args.workspace
                  else f"No workspace matches {args.workspace!r}.")
            args.owner = None
        else:
            workspaces.make_owner(store, target, args.owner, args.name or "")
            print(f"{args.owner} is now an OWNER of '{target.name}'.")
            if args.rename:
                target = workspaces.rename(store, target, args.rename)
                print(f"Renamed it to '{target.name}'.")
            legacy = workspaces.legacy_workspace(store)

    for workspace in store.list_workspaces():
        counts = workspaces.content_counts(store, workspace)
        flags = ["shared" if workspaces.is_shared(store, workspace.id) else "private"]
        if legacy is not None and workspace.id == legacy.id:
            flags.append("owns pre-existing content")
        print(f"\n{workspace.name}  [{', '.join(flags)}]")
        print(f"  id={workspace.id}")
        print(f"  {counts['accounts']} account(s), {counts['instructions']} instruction(s), "
              f"{counts['runs']} run(s)")
        for member in store.list_members(workspace.id):
            print(f"    {member.role.value:6}  {member.email}")
    unassigned_apps = [a for a in store.list_platform_apps() if not a.workspace_id]
    if unassigned_apps:
        print("\nOAuth apps not yet assigned to a workspace:")
        for app in unassigned_apps:
            print(f"  {app.id}  {app.platform.value:10}  {app.label}")
    if args.adopt:
        if legacy is None:
            print("\nNo workspace owns pre-existing content, so there is nothing to assign.")
        else:
            moved = workspaces.adopt_orphans(store, legacy.id)
            print(f"\nAssigned {moved} previously unassigned row(s) to '{legacy.name}'.")
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


def cmd_metrics(args) -> int:
    """Poll recent published posts for fresh performance counters (feedback loop).

    On-demand version of the daily housekeeping sweep. Defaults ``--days`` to a
    positive window even when ``METRICS_REFRESH_DAYS=0`` disables the scheduled
    job, since running this command IS the operator asking for a poll now.
    """
    from . import orchestrator
    from .store import get_store

    configure_logging()
    store = get_store()
    days = args.days if args.days is not None else (settings.metrics_refresh_days or 30)
    result = orchestrator.refresh_metrics(store, max_age_days=days, apply=args.apply)
    verb = "Updated" if args.apply else "Would update"
    print(f"\n{verb} metrics on {result['updated']} run(s) "
          f"({result['polled']} polled, {result['skipped']} skipped) "
          f"of {result['candidates']} candidate(s) in the last {days} day(s).")
    if result.get("settled"):
        # Not an error and not a skip — these are the posts the sweep deliberately
        # did not pay to re-read, which on a pay-per-use API is the point.
        print(f"{result['settled']} post(s) were left alone: old enough that their "
              f"counters have settled, and polled recently enough. Use the run "
              f"page's refresh button to force one.")
    if not args.apply:
        print("\nRe-run with --apply to write the counters.")
    print()
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

    pa = sub.add_parser("assets", help="show local media usage and prune what blob already has")
    pa.add_argument("--apply", action="store_true",
                    help="delete them (default is a dry run)")
    pa.add_argument("--older-than", type=int, default=None, metavar="DAYS",
                    help="retention window (default: ASSET_RETENTION_DAYS)")
    pa.set_defaults(func=cmd_assets)

    pruns = sub.add_parser("runs", help="close out runs stuck on 'running' after a crash")
    pruns.add_argument("--apply", action="store_true",
                       help="write the change (default is a dry run)")
    pruns.add_argument("--older-than", type=int, default=0, metavar="SECONDS",
                       help="how long a run must have been stuck (default: the run "
                            "timeout plus 15 minutes)")
    pruns.set_defaults(func=cmd_runs)

    pw = sub.add_parser("workspaces", help="list workspaces, members and their content")
    pw.add_argument("--owner", metavar="EMAIL",
                    help="make this address an OWNER of a workspace (adds them if needed)")
    pw.add_argument("--workspace", metavar="ID_OR_NAME",
                    help="which workspace --owner applies to "
                         "(default: the one holding pre-existing content)")
    pw.add_argument("--name", metavar="DISPLAY_NAME", default="",
                    help="display name to record alongside --owner")
    pw.add_argument("--rename", metavar="NEW_NAME",
                    help="rename the workspace --owner acted on")
    pw.add_argument("--adopt", action="store_true",
                    help="write a workspace onto rows that predate workspaces (tidy-up only)")
    pw.add_argument("--assign-app", metavar="APP_ID",
                    help="assign an existing OAuth app to --workspace")
    pw.set_defaults(func=cmd_workspaces)

    pr = sub.add_parser("reconcile",
                        help="fix runs marked failed whose post is actually live")
    pr.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    pr.set_defaults(func=cmd_reconcile)

    pm = sub.add_parser("metrics",
                        help="poll recent posts for fresh performance counters")
    pm.add_argument("--apply", action="store_true",
                    help="write the counters (default is a dry run)")
    pm.add_argument("--days", type=int, default=None, metavar="DAYS",
                    help="how far back to poll (default: METRICS_REFRESH_DAYS, "
                         "or 30 when that is 0)")
    pm.set_defaults(func=cmd_metrics)

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
