"""The core guardrail: publish() must honor the instruction's publish mode."""
import asyncio

from aismm.models import (
    Account, Instruction, PlatformName, PublishMode, Run, RunStatus, StagedStatus,
)
from aismm.platforms.base import Capabilities, PublishResult
from aismm.tools.publish_tool import perform_publish


def _state(store, mode, platform=PlatformName.twitter):
    acct = store.upsert_account(Account(platform=platform, handle="bot"), access_token="tok")
    instr = store.upsert_instruction(Instruction(name="i", publish_mode=mode))
    run = store.add_run(Run(instruction_id=instr.id, account_id=acct.id, status=RunStatus.running))
    return {"account": acct, "instruction": instr, "store": store, "run": run, "assets": []}


def test_dry_run_stages_preview_and_never_publishes(store):
    st = _state(store, PublishMode.dry_run)
    res = asyncio.run(perform_publish(st, "hello world", "", "text"))
    assert res["status"] == "staged" and res["mode"] == "dry_run"
    staged = store.list_staged()
    assert len(staged) == 1 and staged[0].status == StagedStatus.preview
    assert store.list_runs()[0].status == RunStatus.staged


def test_approval_queues_pending(store):
    st = _state(store, PublishMode.approval)
    res = asyncio.run(perform_publish(st, "hi", "", "text"))
    assert res["status"] == "pending_approval"
    assert store.list_staged(pending_only=True)[0].status == StagedStatus.pending_approval


def test_live_calls_platform(store, monkeypatch):
    published = {}

    class FakePlatform:
        capabilities = Capabilities(True, True, True, False, "landscape", 280)

        async def publish(self, *, access_token, account, caption, asset_path, media_kind,
                          instruction=None, asset_paths=None, placement="feed"):
            published["called"] = (access_token, caption, media_kind)
            return PublishResult(url="https://x.com/bot/status/1")

    import aismm.platforms.registry as reg
    monkeypatch.setattr(reg, "get_platform", lambda name: FakePlatform())

    st = _state(store, PublishMode.live)
    res = asyncio.run(perform_publish(st, "going live", "", "text"))
    assert res["status"] == "published"
    assert res["url"] == "https://x.com/bot/status/1"
    assert published["called"][0] == "tok"                 # used the stored token
    assert store.list_runs()[0].status == RunStatus.published


def test_unsupported_media_is_rejected(store):
    # YouTube can't post text-only -> publish should refuse without staging.
    st = _state(store, PublishMode.live, platform=PlatformName.youtube)
    res = asyncio.run(perform_publish(st, "just text", "", "text"))
    assert res.get("error") == "unsupported_media"
    assert store.list_staged() == []
