"""Two live posts of the same comic panel — the duplicate-publish guard.

What happened on the account (reconstructed from the service log, where the same
panel is identifiable by its byte sizes: a 3054424-byte PNG converting to a
367815-byte JPEG in two separate runs):

* 04:05 the agent wrote its memory ("attempting panel X"), published
  successfully, and then never wrote the outcome — the run ended after
  ``Instagram published media 18426465187179680`` with no second memory write.
* 05:07 the next scheduled run read that memory, concluded panel X was still
  outstanding, re-fetched the byte-identical image and posted it again.

Plus a second path to the same place: ``media_publish`` returning code 4 *after*
the container reached FINISHED, which we recorded as a failure even though Meta
already held the media and may have published it.

Both fixes are deterministic and live beside the publish-mode gate, because the
fact "this was published" cannot be left to model-written prose.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from aismm import publish_ledger
from aismm.models import Account, Instruction, PlatformName, PublishMode, Run, RunStatus
from aismm.tools.publish_tool import perform_publish

IG_USER = "17841400000000000"
PANEL = b"\xff\xd8\xff" + b"panel-2026-05-17" * 64
OTHER = b"\xff\xd8\xff" + b"panel-2026-05-18" * 64


@pytest.fixture()
def live(store, monkeypatch, tmp_path):
    """A live Instagram instruction with a real asset file, and a fake platform."""
    import dataclasses

    from aismm import assets, config as config_module
    from aismm.platforms import registry

    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))
    monkeypatch.setattr("aismm.tools.publish_tool.media.normalize_image",
                        lambda data, **kw: data)

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="genaicomicbook",
                external_id=IG_USER), access_token="t")
    instruction = store.upsert_instruction(
        Instruction(name="Comicbook", publish_mode=PublishMode.live))
    platform = registry.get_platform(PlatformName.instagram)
    monkeypatch.setattr(registry, "get_platform", lambda *a, **kw: platform)

    calls = []

    async def publish(**kwargs):
        from aismm.platforms.base import PublishResult

        calls.append(kwargs)
        return PublishResult(url="https://www.instagram.com/p/ABC/", external_id="17999",
                             raw={})

    monkeypatch.setattr(platform, "publish", publish)

    def run_publish(data: bytes, caption="Panel 4 of 2026-05-17", placement="feed"):
        path = tmp_path / f"panel-{len(calls)}-{hash(data) & 0xffff}.jpg"
        path.write_bytes(data)
        run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id))
        state = {"account": store.get_account(account.id), "instruction": instruction,
                 "store": store, "run": run, "assets": []}
        result = asyncio.run(perform_publish(state, caption, asset_path=str(path),
                                             media_kind="image", placement=placement))
        return result, run, state

    return run_publish, calls, account, store


# --- the guard ---------------------------------------------------------------------- #

def test_the_same_media_is_not_published_twice(live):
    run_publish, calls, _account, _store = live
    first, _run, _state = run_publish(PANEL)
    assert first["status"] == "published"

    second, run, state = run_publish(PANEL)
    assert second["error"] == "already_published"
    assert len(calls) == 1, "the platform must not be called for a duplicate"
    assert run.status is RunStatus.failed
    assert state["result"]["duplicate"] is True


def test_the_refusal_tells_the_agent_to_advance_its_memory(live):
    """The agent's next move must be to record the item as done, not to retry."""
    run_publish, _calls, _account, _store = live
    run_publish(PANEL)
    second, _run, _state = run_publish(PANEL)
    assert "update_memory" in second["message"]
    assert "ALREADY published" in second["message"]
    assert second["url"] == "https://www.instagram.com/p/ABC/"


def test_different_media_still_publishes(live):
    """The guard keys on content, so the next panel goes out normally."""
    run_publish, calls, _account, _store = live
    run_publish(PANEL)
    second, _run, _state = run_publish(OTHER)
    assert second["status"] == "published"
    assert len(calls) == 2


def test_a_rewritten_caption_does_not_defeat_the_guard(live):
    """The agent writes a fresh caption each run — identity is the MEDIA."""
    run_publish, calls, _account, _store = live
    run_publish(PANEL, caption="Panel 4 — the reveal!")
    second, _run, _state = run_publish(PANEL, caption="A totally different caption")
    assert second["error"] == "already_published"
    assert len(calls) == 1


def test_the_same_image_as_a_story_is_not_a_duplicate_of_the_feed_post(live):
    """Placement is part of the identity: a story of the same art is a real post."""
    run_publish, calls, _account, _store = live
    run_publish(PANEL, placement="feed")
    second, _run, _state = run_publish(PANEL, placement="story")
    assert second["status"] == "published"
    assert len(calls) == 2


def test_the_record_is_written_by_code_not_by_the_agent(live):
    """No update_memory call anywhere in this test — the ledger still has the post.

    This is the actual regression: the agent skipped its post-publish memory write,
    so nothing recorded the success. Now the ledger does, unconditionally.
    """
    run_publish, _calls, account, store = live
    run_publish(PANEL)
    entries = (store.get_account(account.id).meta or {}).get(publish_ledger.META_KEY)
    assert entries and entries[0]["url"] == "https://www.instagram.com/p/ABC/"
    assert entries[0]["id"] == "17999"


# --- the ledger itself -------------------------------------------------------------- #

def test_fingerprint_is_stable_for_identical_bytes(tmp_path, monkeypatch):
    import dataclasses

    from aismm import assets, config as config_module

    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))
    one, two = tmp_path / "a.jpg", tmp_path / "b.jpg"
    one.write_bytes(PANEL)
    two.write_bytes(PANEL)
    assert publish_ledger.fingerprint([str(one)]) == publish_ledger.fingerprint([str(two)])
    three = tmp_path / "c.jpg"
    three.write_bytes(OTHER)
    assert publish_ledger.fingerprint([str(one)]) != publish_ledger.fingerprint([str(three)])


def test_a_carousel_fingerprints_all_of_its_items(tmp_path, monkeypatch):
    import dataclasses

    from aismm import assets, config as config_module

    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))
    one, two = tmp_path / "a.jpg", tmp_path / "b.jpg"
    one.write_bytes(PANEL)
    two.write_bytes(OTHER)
    both = publish_ledger.fingerprint([str(one), str(two)])
    assert both != publish_ledger.fingerprint([str(one)])
    assert both != publish_ledger.fingerprint([str(two)])


def test_an_unreadable_asset_does_not_block_publishing():
    """The guard must fail open — a missing file is the other checks' business."""
    assert publish_ledger.fingerprint(["/no/such/file.jpg"]) == ""


def test_an_empty_fingerprint_never_matches(store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    publish_ledger.record(account, store, "", url="x")
    assert publish_ledger.find(account, "") is None


def test_the_ledger_is_bounded(store, monkeypatch):
    monkeypatch.setattr(publish_ledger, "MAX_ENTRIES", 5)
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    for index in range(12):
        publish_ledger.record(account, store, f"fp-{index}", url=f"u{index}")
    entries = (store.get_account(account.id).meta or {})[publish_ledger.META_KEY]
    assert len(entries) == 5
    assert entries[-1]["fp"] == "fp-11"          # newest kept
    assert publish_ledger.find(account, "fp-0") is None   # oldest dropped


def test_an_expired_entry_stops_blocking(store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    account.set_meta({publish_ledger.META_KEY: [{"fp": "fp", "at": old}]})
    assert publish_ledger.find(account, "fp") is None
    assert publish_ledger.find(account, "fp", ttl_days=200) is not None


def test_the_ledger_survives_a_reload_of_the_account(store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    publish_ledger.record(account, store, "fp-1", url="https://i/p/1")
    assert publish_ledger.find(store.get_account(account.id), "fp-1")


# --- reconciling an ambiguous media_publish failure --------------------------------- #

def _ig():
    from aismm.platforms import registry

    return registry.get_platform(PlatformName.instagram)


def _graph_reply(monkeypatch, recent):
    """Stub the /media read that reconciliation performs."""

    class _Response:
        status_code = 200

        def json(self):
            return {"data": recent}

    class _Client:
        async def get(self, *args, **kwargs):
            return _Response()

    return _ig(), _Client()


def test_a_publish_that_actually_landed_is_detected(monkeypatch):
    """code=4 after FINISHED does not mean nothing was posted."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    platform, client = _graph_reply(monkeypatch, [
        {"id": "17999", "caption": "Panel 4 of 2026-05-17\n\nMade with AI",
         "permalink": "https://www.instagram.com/p/ABC/", "timestamp": now},
    ])
    found = asyncio.run(platform._find_recent_published(
        client, IG_USER, "t", "Panel 4 of 2026-05-17\n\nMade with AI"))
    assert found and found["permalink"] == "https://www.instagram.com/p/ABC/"


def test_an_unrelated_recent_post_is_not_mistaken_for_ours(monkeypatch):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    platform, client = _graph_reply(monkeypatch, [
        {"id": "1", "caption": "Something else entirely",
         "permalink": "https://i/p/1", "timestamp": now},
    ])
    assert asyncio.run(platform._find_recent_published(
        client, IG_USER, "t", "Panel 4 of 2026-05-17")) is None


def test_an_older_post_with_the_same_caption_is_not_this_one(monkeypatch):
    """Otherwise a genuine rate-limit failure would look like a success forever."""
    stale = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    platform, client = _graph_reply(monkeypatch, [
        {"id": "1", "caption": "Panel 4", "permalink": "https://i/p/1", "timestamp": stale},
    ])
    assert asyncio.run(platform._find_recent_published(
        client, IG_USER, "t", "Panel 4")) is None


def test_reconciliation_tolerates_whitespace_differences(monkeypatch):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    platform, client = _graph_reply(monkeypatch, [
        {"id": "1", "caption": "Panel 4   of\n2026-05-17", "permalink": "https://i/p/1",
         "timestamp": now},
    ])
    assert asyncio.run(platform._find_recent_published(
        client, IG_USER, "t", "Panel 4 of 2026-05-17")) is not None


def test_a_story_is_not_reconciled_against_feed_posts(monkeypatch):
    """/media does not list stories, so the newest feed post is NOT our story."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    platform, client = _graph_reply(monkeypatch, [
        {"id": "1", "caption": "An unrelated feed post", "permalink": "https://i/p/1",
         "timestamp": now},
    ])
    assert asyncio.run(platform._find_recent_published(client, IG_USER, "t", "")) is None


def test_a_failed_reconciliation_read_returns_none_not_an_error(monkeypatch):
    """Best-effort: if we cannot check, the original failure must surface."""

    class _Client:
        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("no network")

    assert asyncio.run(_ig()._find_recent_published(_Client(), IG_USER, "t", "x")) is None


def test_graph_timestamps_parse(monkeypatch):
    from aismm.platforms.instagram import _parse_graph_time

    assert _parse_graph_time("2026-07-30T12:33:41+0000") is not None
    assert _parse_graph_time("2026-07-30T12:33:41+00:00") is not None
    assert _parse_graph_time("nonsense") is None
    assert _parse_graph_time(None) is None


# --- a reconciled publish is a success, but still backs off -------------------------- #

@pytest.fixture()
def reconciled(store, monkeypatch, tmp_path):
    """A platform whose publish reports 'errored, but the post is live'."""
    import dataclasses

    from aismm import assets, config as config_module
    from aismm.platforms import registry
    from aismm.platforms.base import PublishResult

    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))
    monkeypatch.setattr("aismm.tools.publish_tool.media.normalize_image",
                        lambda data, **kw: data)

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="genaicomicbook",
                external_id=IG_USER), access_token="t")
    instruction = store.upsert_instruction(
        Instruction(name="Comicbook", publish_mode=PublishMode.live))
    platform = registry.get_platform(PlatformName.instagram)
    monkeypatch.setattr(registry, "get_platform", lambda *a, **kw: platform)

    async def publish(**kwargs):
        return PublishResult(
            url="https://www.instagram.com/p/ABC/", external_id="17999",
            raw={"reconciled": True,
                 "publish_error": "Instagram Graph 403: Application request limit reached"})

    monkeypatch.setattr(platform, "publish", publish)

    path = tmp_path / "panel.jpg"
    path.write_bytes(PANEL)
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id))
    state = {"account": store.get_account(account.id), "instruction": instruction,
             "store": store, "run": run, "assets": []}
    result = asyncio.run(perform_publish(state, "Panel 4", asset_path=str(path),
                                         media_kind="image"))
    return result, run, store.get_account(account.id), store


def test_a_reconciled_publish_is_recorded_as_published(reconciled):
    result, run, _account, _store = reconciled
    assert result["status"] == "published"
    assert run.status is RunStatus.published
    assert run.external_url == "https://www.instagram.com/p/ABC/"
    assert not run.error


def test_a_reconciled_publish_still_starts_a_cooldown(reconciled):
    """The post got through, but Meta signalled a limit — do not knock again."""
    from aismm import cooldown

    _result, _run, account, _store = reconciled
    assert cooldown.is_active(account)


def test_a_reconciled_publish_says_so_in_the_run_log(reconciled):
    _result, run, _account, _store = reconciled
    assert "RECONCILED" in run.log
    assert "is live" in run.log


def test_a_reconciled_publish_is_in_the_ledger(reconciled):
    """So the next run cannot post it again."""
    _result, _run, account, _store = reconciled
    entries = (account.meta or {}).get(publish_ledger.META_KEY)
    assert entries and entries[0]["url"] == "https://www.instagram.com/p/ABC/"


# --- repairing runs already stored as failures --------------------------------------- #

@pytest.fixture()
def to_repair(store, monkeypatch, tmp_path):
    """Two failed runs whose captions match posts that are really on the account."""
    import dataclasses

    from aismm import assets, config as config_module, reconcile

    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, handle="genaicomicbook",
                external_id=IG_USER), access_token="t")
    instruction = store.upsert_instruction(Instruction(name="Comicbook"))

    asset = tmp_path / "panel.jpg"
    asset.write_bytes(PANEL)

    published = store.add_run(Run(
        instruction_id=instruction.id, account_id=account.id, status=RunStatus.failed,
        caption="The rewritten oath is spoken in word and action together.",
        asset_path=str(asset), error="Instagram Graph 403: Application request limit reached"))
    really_failed = store.add_run(Run(
        instruction_id=instruction.id, account_id=account.id, status=RunStatus.failed,
        error="No postable media asset was attached in this run."))

    monkeypatch.setattr(reconcile, "_published_posts", lambda a, s: [
        {"id": "17999", "permalink": "https://www.instagram.com/p/ABC/",
         "caption": "The rewritten oath is spoken in word and action together.",
         "timestamp": "2026-07-30T12:33:41+0000"},
    ])
    return reconcile, account, store, published, really_failed


def test_a_dry_run_reports_without_changing_anything(to_repair):
    reconcile, account, store, published, _failed = to_repair
    report = reconcile.reconcile_account(account, store)
    assert [r[0] for r in report["repaired"]] == [published.id]
    assert report["applied"] is False
    assert store.get_run(published.id).status is RunStatus.failed


def test_applying_marks_the_run_published_with_its_permalink(to_repair):
    reconcile, account, store, published, _failed = to_repair
    reconcile.reconcile_account(account, store, apply=True)
    fixed = store.get_run(published.id)
    assert fixed.status is RunStatus.published
    assert fixed.external_url == "https://www.instagram.com/p/ABC/"
    assert not fixed.error
    assert "RECONCILED" in fixed.log


def test_a_genuinely_failed_run_is_left_alone(to_repair):
    """The 'No postable media' run has no caption and no live post — still failed."""
    reconcile, account, store, _published, really_failed = to_repair
    reconcile.reconcile_account(account, store, apply=True)
    assert store.get_run(really_failed.id).status is RunStatus.failed


def test_repairing_seeds_the_duplicate_guard(to_repair):
    """The whole point: the next run must not post this a third time."""
    reconcile, account, store, published, _failed = to_repair
    report = reconcile.reconcile_account(account, store, apply=True)
    assert report["ledger_seeded"] == 1

    reloaded = store.get_account(account.id)
    entries = (reloaded.meta or {}).get(publish_ledger.META_KEY)
    assert entries and entries[0]["url"] == "https://www.instagram.com/p/ABC/"
    # And the fingerprint really is the media that run published.
    digest = publish_ledger.fingerprint([store.get_run(published.id).asset_path])
    assert publish_ledger.find(reloaded, digest)


def test_repairing_twice_does_not_duplicate_ledger_entries(to_repair):
    reconcile, account, store, _published, _failed = to_repair
    reconcile.reconcile_account(account, store, apply=True)
    second = reconcile.reconcile_account(store.get_account(account.id), store, apply=True)
    entries = (store.get_account(account.id).meta or {})[publish_ledger.META_KEY]
    assert len(entries) == 1
    assert second["ledger_seeded"] == 0


def test_an_account_with_no_token_is_skipped_not_crashed(store, monkeypatch):
    from aismm import reconcile

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="")
    assert reconcile._published_posts(account, store) == []


def test_non_instagram_accounts_are_ignored(store):
    from aismm import reconcile

    account = store.upsert_account(
        Account(platform=PlatformName.twitter, external_id="x"), access_token="t")
    assert reconcile._published_posts(account, store) == []
    assert reconcile.reconcile_all(store) == []
