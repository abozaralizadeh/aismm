"""Which X endpoint is failing, and is it us or X?

``X API 503: Service Unavailable`` says nothing about *where* it came from, and
the answer changes what to do:

* every endpoint 503s          -> X is down for this app. Wait; nothing to fix.
* only media upload 503s       -> post text-only meanwhile, or wait.
* only POST /2/tweets 503s     -> the media is fine; republish when it clears.
* 401/403 instead              -> the token or the app, not X. Reconnect.
* 402                          -> billing. Buy credits at console.x.com.

This probes each step in the order a publish uses them, **without posting
anything**: it reads the profile, then starts a media upload and abandons it
before FINALIZE, so no tweet and no finished media can result.

    python scripts/diagnose_x.py                 # every connected X account
    python scripts/diagnose_x.py --handle abo0zar
    python scripts/diagnose_x.py --repeat 5      # is it intermittent?
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from aismm import tokens  # noqa: E402
from aismm.logging_setup import configure_logging  # noqa: E402
from aismm.models import PlatformName  # noqa: E402
from aismm.platforms.twitter import API  # noqa: E402
from aismm.store import get_store  # noqa: E402

# A real 1x1 PNG: initialize validates media_type against the bytes' format.
_PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63fcffff3f0300050001d0a2a4b0"
    "0000000049454e44ae426082"
)


def _trace(response) -> str:
    headers = getattr(response, "headers", {}) or {}
    request_id = (headers.get("x-request-id") or headers.get("x-transaction-id") or "")
    return f"  [request id {request_id}]" if request_id else ""


def _verdict(status: int) -> str:
    if status in (500, 502, 503, 504):
        return "X-SIDE: transient/outage. Nothing in your app or account to fix."
    if status == 402:
        return "BILLING: no API credits. console.x.com."
    if status in (401, 403):
        return "TOKEN/APP: reconnect the account or check the app's permissions."
    if status == 429:
        return "RATE LIMITED: wait."
    if status >= 400:
        return "REQUEST: the call itself was rejected."
    return "OK"


async def _probe(account, token: str, *, repeat: int) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        checks = [
            ("GET  /2/users/me            (read)",
             lambda: client.get(f"{API}/users/me", headers=headers)),
            ("POST /2/media/upload/initialize (media)",
             lambda: client.post(f"{API}/media/upload/initialize", headers=headers,
                                 json={"media_type": "image/png",
                                       "total_bytes": len(_PIXEL),
                                       "media_category": "tweet_image"})),
        ]
        for label, call in checks:
            for attempt in range(1, repeat + 1):
                started = time.monotonic()
                try:
                    response = await call()
                except Exception as exc:  # noqa: BLE001
                    print(f"  {label}  NETWORK ERROR: {type(exc).__name__}: {exc}")
                    continue
                took = (time.monotonic() - started) * 1000
                suffix = f" (attempt {attempt}/{repeat})" if repeat > 1 else ""
                print(f"  {label}  HTTP {response.status_code}  {took:.0f}ms{suffix}"
                      f"{_trace(response)}")
                print(f"      -> {_verdict(response.status_code)}")
                if response.status_code >= 400:
                    body = (response.text or "")[:200].replace("\n", " ")
                    if body:
                        print(f"      body: {body}")
                if attempt < repeat:
                    await asyncio.sleep(2)
    print("\n  Nothing was posted, and no media upload was finalized.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--handle", help="only this X account")
    parser.add_argument("--repeat", type=int, default=1,
                        help="probe each endpoint N times, 2s apart (is it intermittent?)")
    args = parser.parse_args()

    configure_logging()
    store = get_store()
    accounts = [a for a in store.list_accounts() if a.platform is PlatformName.twitter]
    if args.handle:
        accounts = [a for a in accounts if a.handle == args.handle.lstrip("@")]
    if not accounts:
        print("No X account is connected (or none matched --handle).")
        return 1

    for account in accounts:
        who = account.handle or account.external_id
        print(f"\n=== @{who} ===")
        print(f"  token expiry: {tokens.describe_expiry(account)}")
        try:
            token = tokens.valid_access_token_sync(account, store)
        except Exception as exc:  # noqa: BLE001
            print(f"  could not obtain a token: {exc}")
            continue
        if not token:
            print("  no stored access token — reconnect the account.")
            continue
        asyncio.run(_probe(account, token, repeat=max(args.repeat, 1)))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
