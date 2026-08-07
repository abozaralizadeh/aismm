"""Runs list: paging, search, filters, sorting, and the per-run detail page.

The run table only grows, so filtering/sorting/paging happen in the STORE — a
template that loads every row stops working long before the data does.
"""
from datetime import datetime, timedelta, timezone

import pytest

from aismm.dashboard import app as app_module
from aismm.models import Account, Instruction, PlatformName, Run, RunStatus

BASE = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _naive(value):
    """SQLite hands back naive datetimes, Azure Table aware ones — compare both."""
    return value.replace(tzinfo=None)


@pytest.fixture()
def seeded(store):
    """Two instructions, two accounts, and 40 runs across both."""
    accounts = [
        store.upsert_account(Account(platform=PlatformName.instagram, handle="ig-one",
                                     external_id="1"), access_token="t"),
        store.upsert_account(Account(platform=PlatformName.twitter, handle="x-two",
                                     external_id="2"), access_token="t"),
    ]
    instructions = [
        store.upsert_instruction(Instruction(name="Comic crawl")),
        store.upsert_instruction(Instruction(name="Daily news")),
    ]
    for i in range(40):
        store.add_run(Run(
            instruction_id=instructions[i % 2].id,
            account_id=accounts[i % 2].id,
            status=RunStatus.published if i % 3 else RunStatus.failed,
            caption=f"caption number {i}" if i % 3 else "",
            error="" if i % 3 else f"something broke on run {i}",
            log=f"log line for run {i}",
            created_at=BASE + timedelta(hours=i),
        ))
    return {"accounts": accounts, "instructions": instructions}


@pytest.fixture()
def dash(store, monkeypatch):
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


# --- store: paging ------------------------------------------------------------------ #

def test_paging_returns_distinct_slices(store, seeded):
    first = store.list_runs(limit=10, offset=0)
    second = store.list_runs(limit=10, offset=10)
    assert len(first) == len(second) == 10
    assert not ({r.id for r in first} & {r.id for r in second})


def test_count_matches_the_unpaged_total(store, seeded):
    assert store.count_runs() == 40
    assert len(store.list_runs(limit=1000)) == 40


def test_offset_past_the_end_is_empty(store, seeded):
    assert store.list_runs(limit=10, offset=500) == []


# --- store: sorting ----------------------------------------------------------------- #

def test_default_sort_is_newest_first(store, seeded):
    runs = store.list_runs(limit=5)
    assert runs == sorted(runs, key=lambda r: r.created_at, reverse=True)
    assert _naive(runs[0].created_at) == _naive(BASE + timedelta(hours=39))


def test_ascending_sort(store, seeded):
    runs = store.list_runs(limit=5, descending=False)
    assert _naive(runs[0].created_at) == _naive(BASE)


def test_sort_by_status(store, seeded):
    runs = store.list_runs(limit=1000, sort="status", descending=False)
    assert [r.status.value for r in runs] == sorted(r.status.value for r in runs)


def test_unknown_sort_key_falls_back_to_created_at(store, seeded):
    """A query parameter must not reach arbitrary columns."""
    runs = store.list_runs(limit=5, sort="; DROP TABLE run")
    assert _naive(runs[0].created_at) == _naive(BASE + timedelta(hours=39))


# --- store: filters ------------------------------------------------------------------ #

def test_filter_by_status(store, seeded):
    failed = store.list_runs(limit=1000, status=RunStatus.failed)
    assert failed and all(r.status is RunStatus.failed for r in failed)
    assert store.count_runs(status=RunStatus.failed) == len(failed)


def test_filter_by_instruction(store, seeded):
    target = seeded["instructions"][0].id
    runs = store.list_runs(limit=1000, instruction_id=target)
    assert runs and all(r.instruction_id == target for r in runs)


def test_filter_by_account(store, seeded):
    target = seeded["accounts"][1].id
    runs = store.list_runs(limit=1000, account_id=target)
    assert runs and all(r.account_id == target for r in runs)


def test_filters_combine(store, seeded):
    runs = store.list_runs(limit=1000, status=RunStatus.failed,
                           instruction_id=seeded["instructions"][0].id)
    assert all(r.status is RunStatus.failed
               and r.instruction_id == seeded["instructions"][0].id for r in runs)


# --- store: search -------------------------------------------------------------------- #

def test_search_matches_a_caption(store, seeded):
    runs = store.list_runs(limit=1000, search="caption number 7")
    assert [r.caption for r in runs] == ["caption number 7"]


def test_search_matches_an_error(store, seeded):
    runs = store.list_runs(limit=1000, search="something broke on run 3")
    assert runs and "broke on run 3" in runs[0].error


def test_search_matches_the_log(store, seeded):
    assert store.list_runs(limit=1000, search="log line for run 12")


def test_search_matches_the_instruction_name(store, seeded):
    """The run row stores an id; a human searches for the name."""
    runs = store.list_runs(limit=1000, search="Comic crawl")
    assert runs
    assert all(r.instruction_id == seeded["instructions"][0].id for r in runs)


def test_search_is_case_insensitive(store, seeded):
    assert store.list_runs(limit=1000, search="COMIC CRAWL")


def test_search_narrows_the_count_too(store, seeded):
    assert store.count_runs(search="Comic crawl") == 20


def test_search_with_no_hits(store, seeded):
    assert store.list_runs(limit=1000, search="zzz-nothing") == []
    assert store.count_runs(search="zzz-nothing") == 0


# --- dashboard --------------------------------------------------------------------- #

def test_first_page_shows_only_one_page_of_runs(dash, seeded):
    page = dash.test_client().get("/runs?per_page=25").get_data(as_text=True)
    assert page.count("details →") == 25
    assert "Page 1 of 2" in page
    assert "Refreshes every 30 seconds" in page
    assert "window.location.reload(); }, 30000)" in page


def test_second_page_shows_the_rest(dash, seeded):
    page = dash.test_client().get("/runs?per_page=25&page=2").get_data(as_text=True)
    assert page.count("details →") == 15


def test_page_beyond_the_end_clamps(dash, seeded):
    response = dash.test_client().get("/runs?page=999")
    assert response.status_code == 200
    assert "Page 2 of 2" in response.get_data(as_text=True)


def test_bad_query_parameters_do_not_500(dash, seeded):
    for query in ("page=abc", "per_page=nonsense", "per_page=99999",
                  "status=not-a-status", "sort=evil"):
        assert dash.test_client().get(f"/runs?{query}").status_code == 200


def test_filtering_by_status_in_the_ui(dash, seeded):
    page = dash.test_client().get("/runs?status=failed&per_page=100").get_data(as_text=True)
    assert "badge-published" not in page
    assert "badge-failed" in page


def test_search_in_the_ui(dash, seeded):
    page = dash.test_client().get("/runs?q=caption+number+7").get_data(as_text=True)
    assert "caption number 7" in page
    assert "caption number 8" not in page


def test_sort_links_preserve_the_active_filters(dash, seeded):
    page = dash.test_client().get("/runs?status=failed&q=broke").get_data(as_text=True)
    assert "status=failed" in page and "q=broke" in page


def test_empty_result_offers_to_clear_filters(dash, seeded):
    page = dash.test_client().get("/runs?q=zzz-nothing").get_data(as_text=True)
    assert "No runs match these filters" in page


# --- run detail -------------------------------------------------------------------- #

def test_detail_page_shows_log_error_and_context(dash, store, seeded):
    run = store.list_runs(limit=1000, status=RunStatus.failed)[0]
    page = dash.test_client().get(f"/runs/{run.id}").get_data(as_text=True)
    assert run.error in page
    assert run.log in page
    assert run.id in page
    assert "Comic crawl" in page or "Daily news" in page      # instruction name
    assert "journalctl" in page                               # how to dig further


def test_detail_page_links_back_to_filtered_lists(dash, store, seeded):
    run = store.list_runs(limit=1)[0]
    page = dash.test_client().get(f"/runs/{run.id}").get_data(as_text=True)
    assert f"instruction={run.instruction_id}" in page
    assert f"account={run.account_id}" in page


def test_unknown_run_is_404(dash, seeded):
    assert dash.test_client().get("/runs/does-not-exist").status_code == 404


def test_detail_page_survives_deleted_instruction_and_account(dash, store):
    store.add_run(Run(instruction_id="gone", account_id="also-gone", log="orphan"))
    run = store.list_runs(limit=1)[0]
    response = dash.test_client().get(f"/runs/{run.id}")
    assert response.status_code == 200
    assert "deleted" in response.get_data(as_text=True)


# --- approval bookkeeping ------------------------------------------------------------ #

def test_approving_updates_the_right_run_however_old(store, seeded, monkeypatch):
    """It used to scan only the newest 200 runs, so an old approval silently missed."""
    from aismm import orchestrator
    from aismm.models import StagedPost, StagedStatus

    oldest = store.list_runs(limit=1000, descending=False)[0]
    staged = store.add_staged(StagedPost(instruction_id=oldest.instruction_id,
                                         account_id=oldest.account_id, run_id=oldest.id))
    orchestrator._record_published_run(store, staged, "https://example.com/p/1")

    updated = store.get_run(oldest.id)
    assert updated.status is RunStatus.published
    assert updated.external_url == "https://example.com/p/1"


# --- engagement: staged reply cards render differently ------------------------------- #

def test_a_staged_reply_shows_the_comment_and_an_approve_reply_button(dash, store, seeded):
    """An engagement reply has no media; the card shows what it answers instead."""
    from aismm.models import StagedPost, StagedStatus

    instr = seeded["instructions"][0]
    acct = seeded["accounts"][0]
    store.add_staged(StagedPost(
        instruction_id=instr.id, account_id=acct.id, media_kind="text",
        action_type="reply", target_type="comment", target_id="c1",
        target_excerpt="Where can I buy this?", caption="Link is in our bio!",
        status=StagedStatus.pending_approval))

    page = dash.test_client().get("/runs").get_data(as_text=True)
    assert "Where can I buy this?" in page      # the comment being answered
    assert "Link is in our bio!" in page        # the proposed reply
    assert "Approve &amp; reply" in page         # not "Approve & publish"


def test_a_staged_post_still_shows_approve_and_publish(dash, store, seeded):
    from aismm.models import StagedPost, StagedStatus

    instr = seeded["instructions"][0]
    acct = seeded["accounts"][0]
    store.add_staged(StagedPost(instruction_id=instr.id, account_id=acct.id,
                                caption="a normal post", media_kind="text",
                                status=StagedStatus.pending_approval))
    page = dash.test_client().get("/runs").get_data(as_text=True)
    assert "Approve &amp; publish" in page


def test_approving_a_reply_flashes_success_not_an_error(dash, store, seeded, monkeypatch):
    """A staged reply returns status='replied'; the flash must read it as success
    (it used to only accept 'published', so a sent reply showed a red error box)."""
    from aismm.models import StagedPost, StagedStatus

    instr = seeded["instructions"][1]           # the X account instruction
    acct = seeded["accounts"][1]
    staged = store.add_staged(StagedPost(
        instruction_id=instr.id, account_id=acct.id, media_kind="text",
        action_type="reply", target_type="tweet", target_id="t1",
        target_excerpt="nice thread", caption="thanks!",
        status=StagedStatus.pending_approval))

    monkeypatch.setattr("aismm.orchestrator.get_store", lambda: store)
    monkeypatch.setattr("aismm.tokens.valid_access_token_sync", lambda *a, **k: "tok")

    class _Platform:
        async def reply_to_target(self, access_token, account, *, target_type, target_id, text):
            return {"id": "r1", "url": "https://x.com/abo0zar/status/2085"}

    monkeypatch.setattr("aismm.orchestrator.get_platform", lambda name: _Platform())

    client = dash.test_client()
    resp = client.post(f"/staged/{staged.id}/approve", follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "Reply sent." in page
    assert "https://x.com/abo0zar/status/2085" in page
    assert "flash-error" not in page and "'status': 'replied'" not in page
