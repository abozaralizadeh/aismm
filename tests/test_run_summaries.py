"""List views read run SUMMARIES: one cached read, without prompt and log.

The Azure store reads the whole run table for every list (Table Storage cannot
filter or page server-side), and every run row carries its full prompt and log. A
dashboard tab left open on /runs reloaded every 30 seconds and did three such
reads each time, which came to hundreds of GB a month of cross-region reads.
"""
from datetime import datetime, timedelta, timezone

import pytest

from aismm.models import Run, RunStatus
from aismm.store import azure_store as az
from aismm.store.azure_store import AzureStore
from tests.test_azure_store import FakeTableClient

BIG = "x" * 20_000


@pytest.fixture()
def azure():
    store = AzureStore(table_client=FakeTableClient())
    now = datetime.now(timezone.utc)
    for i in range(6):
        store.add_run(Run(instruction_id="i1", account_id="a1", caption=f"post {i}",
                          status=RunStatus.published if i % 2 else RunStatus.failed,
                          external_id=f"ext{i}" if i % 2 else "", log=f"log {i} {BIG}",
                          prompt=f"prompt {i} {BIG}",
                          created_at=now - timedelta(hours=i)))
    store._table.queries.clear()
    return store


def _run_reads(store):
    return [q for q in store._table.queries if q[0].startswith("PartitionKey eq 'run'")]


def test_summaries_leave_out_prompt_and_log(azure):
    rows, total = azure.run_summaries(limit=3)
    assert total == 6 and len(rows) == 3
    assert all(r.prompt == "" and r.log == "" for r in rows)
    assert [r.caption for r in rows] == ["post 0", "post 1", "post 2"]  # newest first
    (_filter, select), = _run_reads(azure)
    assert "prompt" not in select and "log" not in select
    assert "caption" in select and "RowKey" in select


def test_a_page_its_total_and_the_counts_cost_one_read(azure):
    azure.run_summaries(limit=25)
    azure.count_runs()
    azure.count_runs(status=RunStatus.published)
    azure.count_runs(status=RunStatus.running)
    azure.published_run_summaries()
    assert len(_run_reads(azure)) == 1


def test_a_run_written_here_shows_at_once(azure):
    azure.count_runs()
    run = azure.add_run(Run(instruction_id="i1", account_id="a1", caption="new"))
    assert azure.count_runs() == 7
    run.status = RunStatus.published
    azure.update_run(run)
    assert azure.count_runs(status=RunStatus.running) == 0
    azure.delete_runs_for_account("a1")
    assert azure.count_runs() == 0


def test_the_cache_expires(azure, monkeypatch):
    azure.count_runs()
    monkeypatch.setattr(az, "RUN_SUMMARY_TTL_SECONDS", 0.0)
    azure.count_runs()
    assert len(_run_reads(azure)) == 2


def test_a_caller_cannot_change_what_the_next_caller_sees(azure):
    rows, _ = azure.run_summaries(limit=1)
    rows[0].caption = "edited in memory"
    again, _ = azure.run_summaries(limit=1)
    assert again[0].caption == "post 0"


def test_a_missing_property_reads_as_its_default_not_none(azure):
    # The service returns a selected property an entity lacks as null.
    row = next(v for (pk, _), v in azure._table.rows.items() if pk == "run")
    row.pop("caption")
    azure._forget_run_summaries()
    rows, _ = azure.run_summaries(limit=10)
    assert all(r.caption is not None for r in rows)


def test_search_still_covers_the_log(azure):
    rows, total = azure.run_summaries(search="log 3")
    assert total == 1 and rows[0].caption == "post 3"
    assert azure.count_runs(search="log 3") == 1


def test_published_summaries_match_the_full_query(azure):
    since = datetime.now(timezone.utc) - timedelta(days=1)
    light = [r.id for r in azure.published_run_summaries(since=since)]
    full = [r.id for r in azure.recent_published_runs(since=since)]
    assert light == full and len(light) == 3


def test_list_runs_still_returns_full_rows(azure):
    """Callers of list_runs may write a run back, so it must never be a summary."""
    runs = azure.list_runs(limit=10)
    assert all(r.log.startswith("log") and r.prompt.startswith("prompt") for r in runs)


def test_a_status_filter_is_applied_by_the_table(azure):
    azure.list_runs(status=RunStatus.failed)
    (query, select), = _run_reads(azure)
    assert "status eq 'failed'" in query and select is None
    assert all(r.status is RunStatus.failed and r.log
               for r in azure.list_runs(status=RunStatus.failed))


def test_the_default_on_sqlite_is_list_plus_count(store):
    for i in range(4):
        store.add_run(Run(instruction_id="i1", account_id="a1", caption=f"p{i}"))
    rows, total = store.run_summaries(limit=2)
    assert total == 4 and [r.id for r in rows] == [r.id for r in store.list_runs(limit=2)]
    assert store.published_run_summaries() == store.recent_published_runs()
