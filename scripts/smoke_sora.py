"""Smoke test: generate one short Sora 2 clip and save it under data/assets/.

    python scripts/smoke_sora.py            # generate a clip
    python scripts/smoke_sora.py --pool     # just print the resource pool, no API calls

Skips gracefully (exit 0) if no Sora resource is configured.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run without `pip install -e .`

from aismm.assets import save_bytes  # noqa: E402
from aismm.tools import sora_config
from aismm.tools.sora_client import create_clip_with_failover


def _host(endpoint: str) -> str:
    return endpoint.split("://", 1)[-1].split("/", 1)[0]


def print_pool() -> None:
    """Show how the comma-separated endpoints/keys got aligned into resources."""
    pool = sora_config.pool()
    print(f"Sora pool: {len(pool)} resource(s), up to {sora_config.max_attempts()} tried per clip")
    for i, r in enumerate(pool):
        key = r["key"]
        masked = f"{key[:4]}…{key[-2:]} ({len(key)} chars)" if key else "MISSING"
        print(f"  [{i}] {_host(r['endpoint']):45} model={r['model']:10} key={masked}")


async def main() -> None:
    if not sora_config.enabled():
        print("Sora not configured (AZURE_OPENAI_ENDPOINT_SORA / _KEY_SORA unset) — skipping.")
        return
    print_pool()
    if "--pool" in sys.argv:
        return
    print("\nRequesting a 4s portrait clip from Sora 2…")
    mp4, job_id, resource = await create_clip_with_failover(
        "A calm sunrise timelapse over a quiet ocean horizon, soft warm light.",
        seconds=4, size=sora_config.SIZE_PORTRAIT,
    )
    path = save_bytes(mp4, "mp4")
    print(f"Served by {_host(resource['endpoint'])} (job {job_id})")
    print(f"Saved {len(mp4):,} bytes -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
