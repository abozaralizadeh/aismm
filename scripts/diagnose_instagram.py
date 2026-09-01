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
    try:
        dms = await platform.list_dms(token, account, limit=25)
        print(f"  DMs             : {len(dms)} inbound message(s)")
        for dm in dms[:5]:
            print(f"      · {dm.get('sender') or dm.get('sender_id')}: "
                  f"{(dm.get('text') or '')[:70]!r}  (id={dm.get('id')})")
        if not dms:
            print("      (an EMPTY list here means Instagram really returned no inbound "
                  "messages — the call itself succeeded)")
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

    failures = sum([await _check(a) for a in accounts])
    print("\nNothing failed." if not failures
          else f"\n{failures} check(s) failed — the message above is Instagram's own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
