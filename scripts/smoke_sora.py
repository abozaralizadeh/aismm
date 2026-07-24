"""Smoke test: generate one short Sora 2 clip and save it under data/assets/.

    python scripts/smoke_sora.py

Skips gracefully (exit 0) if no Sora resource is configured.
"""
import asyncio

from aismm.assets import save_bytes
from aismm.tools import sora_config
from aismm.tools.sora_client import generate_video_bytes


async def main() -> None:
    if not sora_config.enabled():
        print("Sora not configured (AZURE_OPENAI_ENDPOINT_SORA / _KEY_SORA unset) — skipping.")
        return
    print("Requesting a 4s portrait clip from Sora 2…")
    mp4 = await generate_video_bytes(
        "A calm sunrise timelapse over a quiet ocean horizon, soft warm light.",
        seconds=4, size=sora_config.SIZE_PORTRAIT,
    )
    path = save_bytes(mp4, "mp4")
    print(f"Saved {len(mp4):,} bytes -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
