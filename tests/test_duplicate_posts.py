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
ACCOUNT = Account(platform=PlatformName.instagram, external_id=IG_USER)
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

    async def post_exists(access_token, account, external_id):
        return True          # the earlier post really is still on the account

    monkeypatch.setattr(platform, "publish", publish)
    # Without this the guard would call the real Graph API to verify a refusal.
    monkeypatch.setattr(platform, "post_exists", post_exists)

    def run_publish(data, caption="Panel 4 of 2026-05-17", placement="feed"):
        """Publish one image, or a carousel when ``data`` is a list of byte strings.

        Each call writes to a FRESH path, mirroring the real agent: it re-downloads
        the panel every run, so identical content lands at a different filename.
        The guard must key on the bytes, never the path.
        """
        blobs = data if isinstance(data, list) else [data]
        paths = []
        for index, blob in enumerate(blobs):
            path = tmp_path / f"panel-{len(calls)}-{index}-{hash(blob) & 0xffff}.jpg"
            path.write_bytes(blob)
            paths.append(str(path))

        run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id))
        state = {"account": store.get_account(account.id), "instruction": instruction,
                 "store": store, "run": run, "assets": []}
        result = asyncio.run(perform_publish(state, caption, asset_path=paths[0],
                                             media_kind="image", placement=placement,
                                             asset_paths=paths if len(paths) > 1 else None))
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
    assert "ALREADY live" in second["message"]
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


def test_a_carousel_yields_one_fingerprint_per_item(tmp_path, monkeypatch):
    """NOT one combined digest for the whole post.

    Hashing all items together is what let the duplicate through: a panel posted
    alone and then re-posted as item 1 of a carousel produced two different
    combined digests, so the guard never fired.
    """
    import dataclasses

    from aismm import assets, config as config_module

    monkeypatch.setattr(assets, "settings",
                        dataclasses.replace(config_module.settings, data_dir=tmp_path))
    one, two = tmp_path / "a.jpg", tmp_path / "b.jpg"
    one.write_bytes(PANEL)
    two.write_bytes(OTHER)

    both = publish_ledger.fingerprints([str(one), str(two)])
    assert len(both) == 2
    # Each item's fingerprint is the SAME whether it is posted alone or in a set.
    assert both[0] == publish_ledger.fingerprints([str(one)])[0]
    assert both[1] == publish_ledger.fingerprints([str(two)])[0]
    assert both[0] != both[1]


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


# --- the carousel hole: an item already posted alone -------------------------------- #
# Live regression. A comic panel was published as a single post (467662-byte JPEG),
# then three hours later published AGAIN as item 1 of a two-photo carousel. Both
# runs recorded a fingerprint and neither matched the other, because the ledger
# hashed every item of a post into ONE digest. The unit a follower sees repeated
# is the ITEM, so that is what must be fingerprinted.

def test_an_item_posted_alone_cannot_return_inside_a_carousel(live):
    run_publish, calls, _account, _store = live
    first, _run, _state = run_publish(PANEL)
    assert first["status"] == "published"

    second, run, state = run_publish([PANEL, OTHER])          # carousel reusing it
    assert second["error"] == "already_published"
    assert len(calls) == 1, "the carousel was published despite reusing a posted panel"
    assert state["result"]["duplicate"] is True


def test_the_refusal_names_which_item_was_the_duplicate(live):
    run_publish, _calls, _account, _store = live
    run_publish(PANEL)
    second, _run, _state = run_publish([OTHER, PANEL])
    assert "Item 2 of 2" in second["message"]
    assert "ALREADY live" in second["message"]


def test_an_item_from_a_carousel_cannot_be_reposted_alone(live):
    """The mirror case — every item of a carousel is remembered individually."""
    run_publish, calls, _account, _store = live
    run_publish([PANEL, OTHER])
    second, _run, _state = run_publish(OTHER)
    assert second["error"] == "already_published"
    assert len(calls) == 1


def test_a_carousel_of_entirely_new_items_still_publishes(live):
    run_publish, calls, _account, _store = live
    run_publish(PANEL)
    third = b"\xff\xd8\xff" + b"panel-2026-05-19" * 64
    second, _run, _state = run_publish([OTHER, third])
    assert second["status"] == "published"
    assert len(calls) == 2


def test_every_carousel_item_lands_in_the_ledger(live):
    run_publish, _calls, account, store = live
    run_publish([PANEL, OTHER])
    entries = (store.get_account(account.id).meta or {})[publish_ledger.META_KEY]
    assert len(entries) == 2
    assert all(e["url"] == "https://www.instagram.com/p/ABC/" for e in entries)


def test_find_any_reports_the_index_of_the_offending_item(store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    publish_ledger.record(account, store, ["fp-b"], url="https://i/p/1")
    found = publish_ledger.find_any(store.get_account(account.id), ["fp-a", "fp-b", "fp-c"])
    assert found is not None
    index, entry = found
    assert index == 1 and entry["url"] == "https://i/p/1"


def test_find_any_is_none_when_nothing_matches(store):
    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    assert publish_ledger.find_any(account, ["fp-a", "fp-b"]) is None
    assert publish_ledger.find_any(account, []) is None


def test_the_same_item_in_a_story_is_still_not_a_feed_duplicate(live):
    """Placement stays part of each item's identity."""
    run_publish, calls, _account, _store = live
    run_publish([PANEL, OTHER], placement="feed")
    second, _run, _state = run_publish(PANEL, placement="story")
    assert second["status"] == "published"
    assert len(calls) == 2


# --- the account is the authority, not the ledger ------------------------------------ #
# The ledger records what we published; it cannot know that a human went and
# deleted a post by hand. Blocking on a stale record would make that content
# unpublishable forever, so a refusal is verified against the live account first.

@pytest.fixture()
def deletable(store, monkeypatch, tmp_path):
    """Like ``live``, but the fake platform tracks which media ids still exist."""
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

    state = {"calls": [], "on_account": set(), "next_id": 1, "answer": "real"}

    async def publish(**kwargs):
        state["calls"].append(kwargs)
        media_id = str(17000 + state["next_id"])
        state["next_id"] += 1
        state["on_account"].add(media_id)
        return PublishResult(url=f"https://i/p/{media_id}", external_id=media_id, raw={})

    async def post_exists(access_token, account, external_id):
        if state["answer"] == "unknown":
            return None
        return external_id in state["on_account"]

    monkeypatch.setattr(platform, "publish", publish)
    monkeypatch.setattr(platform, "post_exists", post_exists)

    def run_publish(data, caption="Panel 4", placement="feed"):
        blobs = data if isinstance(data, list) else [data]
        paths = []
        for index, blob in enumerate(blobs):
            path = tmp_path / f"p-{len(state['calls'])}-{index}-{hash(blob) & 0xffff}.jpg"
            path.write_bytes(blob)
            paths.append(str(path))
        run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id))
        run_state = {"account": store.get_account(account.id), "instruction": instruction,
                     "store": store, "run": run, "assets": []}
        return asyncio.run(perform_publish(
            run_state, caption, asset_path=paths[0], media_kind="image",
            placement=placement, asset_paths=paths if len(paths) > 1 else None))

    return run_publish, state, account, store


def test_a_post_deleted_by_hand_can_be_published_again(deletable):
    """The user's case: they removed the post from Instagram themselves."""
    run_publish, fake, _account, _store = deletable
    first = run_publish(PANEL)
    assert first["status"] == "published"

    fake["on_account"].clear()                    # deleted in the Instagram app
    second = run_publish(PANEL)
    assert second["status"] == "published", "a deleted post must not block re-publishing"
    assert len(fake["calls"]) == 2


def test_the_stale_entry_is_removed_not_just_ignored(deletable):
    """Otherwise every future run pays for the same pointless existence check."""
    run_publish, fake, account, store = deletable
    run_publish(PANEL)
    fake["on_account"].clear()
    run_publish(PANEL)

    entries = (store.get_account(account.id).meta or {})[publish_ledger.META_KEY]
    assert len(entries) == 1, "the dead entry should be gone, replaced by the new one"


def test_a_post_that_is_still_live_is_still_refused(deletable):
    """The guard must not weaken for posts that really are there."""
    run_publish, fake, _account, _store = deletable
    run_publish(PANEL)
    second = run_publish(PANEL)
    assert second["error"] == "already_published"
    assert len(fake["calls"]) == 1


def test_an_inconclusive_check_publishes_rather_than_skipping(deletable):
    """Default is fail-OPEN.

    For sequential content — a comic posted panel by panel — a wrongly skipped
    item breaks the running order and the gap is not recoverable, whereas a
    duplicate is two taps to delete. A CONFIRMED duplicate is still refused.
    """
    run_publish, fake, _account, _store = deletable
    run_publish(PANEL)
    fake["answer"] = "unknown"                    # rate limited / network trouble
    second = run_publish(PANEL)
    assert second["status"] == "published"
    assert len(fake["calls"]) == 2


def test_strict_mode_refuses_an_inconclusive_check(deletable, monkeypatch):
    """PUBLISH_DUPLICATE_GUARD_STRICT=1 restores fail-closed for accounts that
    would rather have a gap than a duplicate."""
    import dataclasses

    from aismm import config as config_module
    from aismm.tools import publish_tool

    monkeypatch.setattr(publish_tool, "settings", dataclasses.replace(
        config_module.settings, publish_duplicate_guard_strict=True))

    run_publish, fake, _account, _store = deletable
    run_publish(PANEL)
    fake["answer"] = "unknown"
    second = run_publish(PANEL)
    assert second["error"] == "already_published"
    assert len(fake["calls"]) == 1


def test_an_unverified_refusal_does_not_tell_the_agent_to_advance(deletable, monkeypatch):
    """The item may never have gone out — advancing would drop it from the run order."""
    import dataclasses

    from aismm import config as config_module
    from aismm.tools import publish_tool

    monkeypatch.setattr(publish_tool, "settings", dataclasses.replace(
        config_module.settings, publish_duplicate_guard_strict=True))

    run_publish, fake, _account, _store = deletable
    run_publish(PANEL)
    fake["answer"] = "unknown"
    second = run_publish(PANEL)
    assert "could not be confirmed" in second["message"]
    assert "Do NOT advance your position" in second["message"]
    assert "update_memory" not in second["message"]


def test_a_confirmed_refusal_says_the_post_is_still_live(deletable):
    """The opposite case: verified, so the agent SHOULD advance past it."""
    run_publish, _fake, _account, _store = deletable
    run_publish(PANEL)
    second = run_publish(PANEL)
    assert "still on the account" in second["message"]
    assert "update_memory" in second["message"]


def test_a_carousel_with_one_deleted_and_one_live_item_is_still_refused(deletable):
    """Only the deleted item is forgiven; the live one still blocks the post."""
    run_publish, fake, _account, _store = deletable
    run_publish(PANEL)
    live_id = next(iter(fake["on_account"]))
    run_publish(OTHER)
    fake["on_account"] = {live_id}                # the OTHER post was deleted

    third = run_publish([OTHER, PANEL])           # OTHER forgiven, PANEL still live
    assert third["error"] == "already_published"
    assert len(fake["calls"]) == 2


def test_an_entry_with_no_media_id_stays_blocking(store, monkeypatch):
    """Nothing to verify against, so the guard stays closed."""
    from aismm.platforms import registry
    from aismm.tools.publish_tool import _confirm_duplicate

    account = store.upsert_account(
        Account(platform=PlatformName.instagram, external_id=IG_USER), access_token="t")
    publish_ledger.record(account, store, ["fp-a"], url="https://i/p/1")   # no external_id
    platform = registry.get_platform(PlatformName.instagram)

    found = asyncio.run(_confirm_duplicate(
        store.get_account(account.id), store, platform, ["fp-a"]))
    assert found is not None


def test_post_exists_reports_gone_for_a_deleted_media_id(monkeypatch):
    """Graph answers a deleted id with code 803 / subcode 33."""
    from aismm.platforms import instagram, registry

    class _Response:
        status_code = 400
        request = httpx.Request("GET", "https://graph.facebook.com/v21.0/1")

        def json(self):
            return {"error": {"message": "Unsupported get request. Object does not exist",
                              "code": 803, "error_subcode": 33, "type": "GraphMethodException"}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(instagram.httpx, "AsyncClient", lambda **kw: _Client())
    platform = registry.get_platform(PlatformName.instagram)
    assert asyncio.run(platform.post_exists("token", ACCOUNT, "1")) is False


def test_post_exists_reports_unknown_for_a_rate_limit(monkeypatch):
    from aismm.platforms import instagram, registry

    class _Response:
        status_code = 403
        request = httpx.Request("GET", "https://graph.facebook.com/v21.0/1")

        def json(self):
            return {"error": {"message": "Application request limit reached", "code": 4}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(instagram.httpx, "AsyncClient", lambda **kw: _Client())
    platform = registry.get_platform(PlatformName.instagram)
    assert asyncio.run(platform.post_exists("token", ACCOUNT, "1")) is None


def test_a_platform_without_the_check_keeps_ledger_only_behaviour():
    """The base default is 'cannot tell', so other platforms are unaffected."""
    from aismm.platforms import registry

    twitter = registry.get_platform(PlatformName.twitter)
    assert asyncio.run(twitter.post_exists("token", ACCOUNT, "1")) is None


# --- archived counts as "not live" --------------------------------------------------- #
# Graph has no is_archived field. An archived post still resolves by id but is
# dropped from the profile listing, so absence from the listing is the signal —
# guarded by a timestamp check so an old-but-live post isn't misread as archived.

def _graph_pair(monkeypatch, media_response, listing_response):
    """Stub the two GETs post_exists makes, in order."""
    from aismm.platforms import instagram

    calls = {"n": 0}

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.request = httpx.Request("GET", "https://graph.facebook.com/v21.0/x")

        def json(self):
            return self._payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            calls["n"] += 1
            status, payload = media_response if calls["n"] == 1 else listing_response
            return _Resp(status, payload)

    monkeypatch.setattr(instagram.httpx, "AsyncClient", lambda **kw: _Client())


def _stamp(hours_ago):
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S+0000")


def test_an_archived_post_is_reported_as_not_live(monkeypatch):
    """Resolves by id, but absent from the grid listing it should appear in."""
    from aismm.platforms import registry

    _graph_pair(
        monkeypatch,
        (200, {"id": "17999", "timestamp": _stamp(5)}),
        (200, {"data": [{"id": "other-1", "timestamp": _stamp(1)},
                        {"id": "other-2", "timestamp": _stamp(20)}]}),
    )
    platform = registry.get_platform(PlatformName.instagram)
    assert asyncio.run(platform.post_exists("token", ACCOUNT, "17999")) is False


def test_a_post_on_the_grid_is_reported_as_live(monkeypatch):
    from aismm.platforms import registry

    _graph_pair(
        monkeypatch,
        (200, {"id": "17999", "timestamp": _stamp(5)}),
        (200, {"data": [{"id": "17999", "timestamp": _stamp(5)},
                        {"id": "other", "timestamp": _stamp(1)}]}),
    )
    platform = registry.get_platform(PlatformName.instagram)
    assert asyncio.run(platform.post_exists("token", ACCOUNT, "17999")) is True


def test_a_post_older_than_the_scanned_window_is_unknown_not_archived(monkeypatch):
    """The paging trap: we simply never looked back far enough to say."""
    from aismm.platforms import registry

    _graph_pair(
        monkeypatch,
        (200, {"id": "17999", "timestamp": _stamp(500)}),      # much older
        (200, {"data": [{"id": "a", "timestamp": _stamp(1)},
                        {"id": "b", "timestamp": _stamp(10)}]}),
    )
    platform = registry.get_platform(PlatformName.instagram)
    assert asyncio.run(platform.post_exists("token", ACCOUNT, "17999")) is None


def test_an_unreadable_listing_leaves_the_post_treated_as_live(monkeypatch):
    """The media itself resolved; without the grid we must not claim it is archived."""
    from aismm.platforms import registry

    _graph_pair(
        monkeypatch,
        (200, {"id": "17999", "timestamp": _stamp(5)}),
        (403, {"error": {"message": "Application request limit reached", "code": 4}}),
    )
    platform = registry.get_platform(PlatformName.instagram)
    assert asyncio.run(platform.post_exists("token", ACCOUNT, "17999")) is True


def test_an_empty_grid_with_no_timestamps_is_unknown(monkeypatch):
    from aismm.platforms import registry

    _graph_pair(
        monkeypatch,
        (200, {"id": "17999", "timestamp": _stamp(5)}),
        (200, {"data": []}),
    )
    platform = registry.get_platform(PlatformName.instagram)
    assert asyncio.run(platform.post_exists("token", ACCOUNT, "17999")) is None
