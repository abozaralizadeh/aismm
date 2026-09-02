"""Ask Instagram, directly, what this app can see for an account.

Written after several rounds of "the engage run says there are no DMs" with no
way to tell an EMPTY inbox from an unreadable one. This calls the same code the
agent's tools call, prints the raw answer, and says what a failure means — no
posting, no replying, no agent.

    python scripts/diagnose_instagram.py                 # every IG account
    python scripts/diagnose_instagram.py --account ID
    python scripts/diagnose_instagram.py --handle genaicomicbook
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aismm import tokens                                    # noqa: E402
from aismm.logging_setup import configure_logging           # noqa: E402
from aismm.models import PlatformName                       # noqa: E402
from aismm.platforms.registry import get_platform           # noqa: E402
from aismm.store import get_store                           # noqa: E402


async def _check(account) -> int:
    platform = get_platform(PlatformName.instagram)
    store = get_store()
    print(f"\n=== @{account.handle or account.external_id} "
          f"({account.external_id}) ===")

    meta = account.meta or {}
    page_id = meta.get("page_id")
    target = platform._messaging_target(account)
    print(f"  IG user id      : {account.external_id}")
    print(f"  Page id         : {page_id or 'NOT RECORDED — reconnect to store it'}")
    print(f"  Messaging goes to: /{target}/conversations   "
          f"{'(the Page)' if page_id else '(me — resolves to the Page from a page token)'}")
    granted = meta.get("granted_scopes") or []
    print(f"  Scopes at connect: {', '.join(sorted(granted)) or 'none recorded'}")
    if granted and "instagram_manage_messages" not in granted:
        print("  ⚠ instagram_manage_messages is NOT granted — reading DMs will be "
              "refused. Get it through App Review, then RECONNECT the account.")

    token = await tokens.valid_access_token(account, store)

    try:
        info = await platform.inspect_token(token, account)
        print(f"  Token           : type={info.get('type')} valid={info.get('is_valid')}")
        if info.get("type") == "USER":
            print("  ⚠ This is a USER token, not a PAGE token. Reconnect and tick the "
                  "Page itself in the dialog.")
    except Exception as exc:  # noqa: BLE001
        print(f"  Token           : could not inspect — {exc}")

    failures = 0

    # The RAW conversation list, before any filtering of ours. This line is what
    # separates "Instagram did not return the thread" from "we dropped it" — a
    # distinction that cost several rounds of guessing.
    try:
        raw = await platform._graph_get(
            token, f"{target}/conversations",
            {"platform": "instagram",
             "fields": "id,updated_time,participants,messages.limit(1){id,from,created_time}",
             "limit": 50})
        threads_raw = raw.get("data") or []
        print(f"  Threads (raw)   : {len(threads_raw)} returned by Graph")
        for convo in threads_raw:
            names = ", ".join(
                str(person.get("username") or person.get("id", "?"))
                for person in ((convo.get("participants") or {}).get("data") or [])) or "?"
            print(f"      · {names}   updated {convo.get('updated_time', '?')}")
        if (raw.get("paging") or {}).get("next"):
            print("      (more pages exist — list_dms follows them)")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"  Threads (raw)   : FAILED — {exc}")

    try:
        dms = await platform.list_dms(token, account, limit=50)
        threads = {d.get("thread_id") or d.get("conversation_id") for d in dms}
        answerable = sum(1 for d in dms if d.get("can_reply"))
        print(f"  DMs             : {len(dms)} inbound message(s) "
              f"across {len(threads)} conversation(s); {answerable} still inside "
              f"Instagram's 24h reply window")
        for dm in dms[:10]:
            age = dm.get("age_hours")
            when = "age unknown" if age is None else f"{age:.0f}h ago"
            mark = "" if dm.get("can_reply") else "  [TOO OLD to answer via API]"
            print(f"      · {dm.get('sender') or dm.get('sender_id')} ({when}): "
                  f"{(dm.get('text') or '')[:60]!r}{mark}")
        if not dms:
            print("      (an EMPTY list here means Instagram really returned no inbound "
                  "messages — the call itself succeeded)")
        print("      NOTE: a thread missing from BOTH lists above was not returned by "
              "Instagram\n            at all. Check, in order: is the DM on THIS account "
              "(run this with no\n            --handle to scan every connected one); is it "
              "sitting unaccepted in\n            Requests or the General tab (a DM from a "
              "non-follower starts in\n            Requests — accept it in the app and it "
              "appears here); has that\n            Requests thread been inactive for 30+ "
              "days, which the API cannot reach.")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"  DMs             : FAILED\n      {exc}")

    try:
        media = await platform.list_media(token, account, limit=5)
        print(f"  Recent media    : {len(media)} post(s)/reel(s) visible")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"  Recent media    : FAILED — {exc}")

    return failures


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="account id")
    parser.add_argument("--handle", help="account handle")
    args = parser.parse_args()

    configure_logging()
    accounts = [a for a in get_store().list_accounts()
                if a.platform is PlatformName.instagram]
    if args.account:
        accounts = [a for a in accounts if a.id == args.account]
    if args.handle:
        wanted = args.handle.lstrip("@").lower()
        accounts = [a for a in accounts if (a.handle or "").lower() == wanted]
    if not accounts:
        print("No matching Instagram account.")
        return 1
    if not (args.account or args.handle) and len(accounts) > 1:
        print(f"Scanning all {len(accounts)} connected Instagram account(s) — a DM you "
              f"cannot find\nis often simply on a different one.")

    failures = sum([await _check(a) for a in accounts])
    print("\nNothing failed." if not failures
          else f"\n{failures} check(s) failed — the message above is Instagram's own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
