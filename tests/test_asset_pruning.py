"""Keeping the local disk as a cache, not an archive.

Reported: the VM filled up. Every asset the agent ever generated was kept on
local disk forever — a Sora clip is tens of MB — and once the disk is full the
next run cannot write its media and neither can anything else.

With blob storage configured the local folder is a cache: ``read_bytes`` already
falls back to blob, so a pruned file is still readable. The safety property is
absolute and is what these tests are mostly about: **a file is only deleted after
the blob copy has been confirmed to exist.**
"""
import dataclasses
import time

import pytest

from aismm import assets
from aismm import config as config_module
from aismm.store import blob_media


@pytest.fixture()
def cache(monkeypatch, tmp_path):
    """A local asset dir, with blob storage stubbed and controllable."""
    patched = dataclasses.replace(config_module.settings, data_dir=tmp_path)
    monkeypatch.setattr(assets, "settings", patched)
    (tmp_path / "assets").mkdir()
    present: set[str] = set()

    monkeypatch.setattr(assets.blob_media, "enabled", lambda: True)
    monkeypatch.setattr(assets.blob_media, "exists", lambda name: name in present)
    return tmp_path / "assets", present


def _file(directory, name, *, age_days=0.0, size=1024):
    path = directory / name
    path.write_bytes(b"x" * size)
    if age_days:
        old = time.time() - age_days * 86400
        import os
        os.utime(path, (old, old))
    return path


# --- the safety property --------------------------------------------------------------- #

def test_a_file_is_deleted_only_when_blob_has_it(cache):
    directory, present = cache
    kept = _file(directory, "local-only.jpg", age_days=90)
    gone = _file(directory, "backed-up.jpg", age_days=90)
    present.add("backed-up.jpg")

    result = assets.prune_local(14, apply=True)
    assert not gone.exists()
    assert kept.exists()                      # the only copy — never touched
    assert result["deleted"] == 1
    assert result["kept_local_only"] == 1


def test_nothing_is_pruned_without_blob_storage(cache, monkeypatch):
    """Without a second copy, pruning is just deleting media."""
    directory, _present = cache
    old = _file(directory, "a.jpg", age_days=365)
    monkeypatch.setattr(assets.blob_media, "enabled", lambda: False)
    result = assets.prune_local(1, apply=True)
    assert old.exists()
    assert "blob storage is not configured" in result["skipped"]


def test_an_unreachable_blob_is_not_a_licence_to_delete(cache, monkeypatch):
    directory, _present = cache
    old = _file(directory, "a.jpg", age_days=90)

    def boom(name):
        raise RuntimeError("network down")

    monkeypatch.setattr(assets.blob_media, "exists", boom)
    result = assets.prune_local(14, apply=True)
    assert old.exists()
    assert result["kept_local_only"] == 1


def test_a_dry_run_deletes_nothing(cache):
    directory, present = cache
    path = _file(directory, "a.jpg", age_days=90)
    present.add("a.jpg")
    result = assets.prune_local(14, apply=False)
    assert path.exists()
    assert result["deleted"] == 1 and result["applied"] is False


# --- what it keeps --------------------------------------------------------------------- #

def test_recent_files_are_left_alone(cache):
    directory, present = cache
    fresh = _file(directory, "today.jpg", age_days=1)
    present.add("today.jpg")
    result = assets.prune_local(14, apply=True)
    assert fresh.exists()
    assert result["skipped_recent"] == 1


def test_named_files_are_spared_whatever_their_age(cache):
    """The media of recent runs: a preview or a republish still wants it local."""
    directory, present = cache
    path = _file(directory, "in-a-recent-run.jpg", age_days=365)
    present.add("in-a-recent-run.jpg")
    assets.prune_local(14, apply=True, keep={"in-a-recent-run.jpg"})
    assert path.exists()


def test_the_freed_bytes_are_reported(cache):
    directory, present = cache
    for name in ("a.jpg", "b.jpg"):
        _file(directory, name, age_days=90, size=5000)
        present.add(name)
    result = assets.prune_local(14, apply=True)
    assert result["deleted"] == 2
    assert result["freed_bytes"] == 10000


def test_usage_is_reported_without_touching_anything(cache):
    directory, _present = cache
    _file(directory, "a.jpg", age_days=3, size=2048)
    usage = assets.local_usage()
    assert usage["files"] == 1 and usage["bytes"] == 2048
    assert usage["oldest_days"] == 3.0
    assert (directory / "a.jpg").exists()


# --- a pruned asset is still readable and still serves --------------------------------- #

def test_a_pruned_asset_is_still_readable(cache, monkeypatch):
    """read_bytes falls back to blob — that is what makes pruning safe."""
    directory, present = cache
    path = _file(directory, "a.jpg", age_days=90)
    present.add("a.jpg")
    monkeypatch.setattr(assets.blob_media, "download", lambda name: b"from-blob")
    assets.prune_local(14, apply=True)
    assert not path.exists()
    assert assets.read_bytes(str(path)) == b"from-blob"


def test_a_pruned_asset_still_counts_as_existing(cache):
    directory, present = cache
    path = _file(directory, "a.jpg", age_days=90)
    present.add("a.jpg")
    assets.prune_local(14, apply=True)
    assert assets.exists(str(path)) is True      # publish must not refuse it


def test_the_dashboard_serves_a_pruned_asset_from_blob(store, monkeypatch, tmp_path):
    """Otherwise tidying the disk would break every old thumbnail and preview."""
    from aismm.config import AuthSettings
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module, assets):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    monkeypatch.setattr(blob_media, "enabled", lambda: True)
    monkeypatch.setattr(blob_media, "download", lambda name: b"blob-bytes")
    (tmp_path / "assets").mkdir(exist_ok=True)

    application = app_module.create_app()
    application.secret_key = "test"
    response = application.test_client().get("/assets/pruned.jpg")
    assert response.status_code == 200
    assert response.data == b"blob-bytes"


def test_a_genuinely_missing_asset_is_still_404(store, monkeypatch, tmp_path):
    from aismm.config import AuthSettings
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module, assets):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    monkeypatch.setattr(blob_media, "enabled", lambda: False)
    (tmp_path / "assets").mkdir(exist_ok=True)

    application = app_module.create_app()
    application.secret_key = "test"
    assert application.test_client().get("/assets/nope.jpg").status_code == 404


# --- it runs on its own, and never blocks the service ---------------------------------- #

def test_the_prune_is_scheduled_and_run_at_boot(monkeypatch):
    from aismm import scheduler

    jobs, ran = [], []
    monkeypatch.setattr(scheduler, "_reap_stale_runs", lambda: None)
    monkeypatch.setattr(scheduler, "refresh_jobs", lambda: None)
    monkeypatch.setattr("aismm.orchestrator.prune_asset_cache",
                        lambda *a, **kw: ran.append(True))

    class _Sched:
        running = True
        def start(self): return None
        def add_job(self, func, trigger, **kw): jobs.append(kw.get("id"))

    monkeypatch.setattr(scheduler, "get_scheduler", lambda: _Sched())
    scheduler.start()
    assert "housekeeping:assets" in jobs
    for _ in range(40):                        # the boot sweep is a daemon thread
        if ran:
            break
        time.sleep(0.05)
    assert ran


def test_a_failing_prune_does_not_stop_the_scheduler(monkeypatch):
    """Posting is the point; tidying the disk is maintenance."""
    from aismm import scheduler

    started = {}
    monkeypatch.setattr(scheduler, "_reap_stale_runs", lambda: None)
    monkeypatch.setattr(scheduler, "refresh_jobs", lambda: started.setdefault("jobs", True))

    class _Sched:
        running = True
        def start(self): return None
        def add_job(self, *a, **kw): raise RuntimeError("scheduler is unhappy")

    monkeypatch.setattr(scheduler, "get_scheduler", lambda: _Sched())
    scheduler.start()
    assert started["jobs"] is True


def test_the_retention_window_is_configurable():
    from aismm.config import settings

    assert settings.asset_retention_days == 14      # ASSET_RETENTION_DAYS


def test_zero_retention_switches_the_prune_off(monkeypatch):
    """An operator zeroing a retention setting is turning it off. Reading it as
    'delete everything' would wipe the cache on the next boot."""
    from aismm import orchestrator

    patched = dataclasses.replace(config_module.settings, asset_retention_days=0)
    monkeypatch.setattr(orchestrator, "settings", patched)
    called = []
    monkeypatch.setattr(assets, "prune_local", lambda *a, **kw: called.append(a))

    result = orchestrator.prune_asset_cache(store=object())
    assert called == []
    assert result["deleted"] == 0
    assert "never pruned" in result["skipped"]


def test_an_explicit_zero_still_clears_everything(monkeypatch, store):
    """The escape hatch: `cli assets --older-than 0 --apply`."""
    from aismm import orchestrator

    seen = {}
    monkeypatch.setattr(assets, "prune_local",
                        lambda days, **kw: seen.setdefault("days", days) or {"deleted": 0})
    orchestrator.prune_asset_cache(store=store, older_than_days=0)
    assert seen["days"] == 0


# --- the UI points at blob, not at us -------------------------------------------------- #
# Once the local folder is a cache, serving every thumbnail through the dashboard
# means the VM reads the file off disk — or pulls it back out of blob and
# re-streams it — on every page render. The browser should fetch media from
# storage directly.

@pytest.fixture()
def container(monkeypatch):
    """A configured blob container whose access level the test controls."""
    state = {"public": True}
    monkeypatch.setattr(blob_media, "enabled", lambda: True)
    monkeypatch.setattr(blob_media, "public_read", lambda: state["public"])
    monkeypatch.setattr(blob_media, "url", lambda name: f"https://acct.blob.core.windows.net/m/{name}")
    return state


def test_media_is_served_from_blob(container):
    assert assets.browser_url("/data/assets/a.jpg").startswith("https://acct.blob.")


def test_a_private_container_falls_back_to_us(container):
    """A blob URL the browser cannot read is a broken preview."""
    container["public"] = False
    assert assets.browser_url("/data/assets/a.jpg") == ""


def test_not_knowing_the_access_level_falls_back_to_us(container):
    """None is 'could not ask', not 'private' — and never worth a broken image."""
    container["public"] = None
    assert assets.browser_url("/data/assets/a.jpg") == ""


def test_without_blob_there_is_nothing_to_point_at(monkeypatch):
    monkeypatch.setattr(blob_media, "enabled", lambda: False)
    assert assets.browser_url("/data/assets/a.jpg") == ""


def test_a_broken_blob_client_does_not_break_the_page(container, monkeypatch):
    def boom(name):
        raise RuntimeError("no client")

    monkeypatch.setattr(blob_media, "url", boom)
    assert assets.browser_url("/data/assets/a.jpg") == ""


@pytest.fixture()
def dashboard(monkeypatch, store, tmp_path):
    from aismm.config import AuthSettings
    from aismm.dashboard import app as app_module
    from aismm.dashboard import sso

    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    for module in (sso, app_module, config_module, assets):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


def test_the_template_helper_prefers_blob(dashboard, container):
    with dashboard.test_request_context("/"):
        rendered = dashboard.jinja_env.from_string(
            "{{ media_url('/data/assets/a.jpg') }}").render()
    assert rendered == "https://acct.blob.core.windows.net/m/a.jpg"


def test_the_template_helper_falls_back_to_our_route(dashboard, monkeypatch):
    monkeypatch.setattr(blob_media, "enabled", lambda: False)
    with dashboard.test_request_context("/"):
        rendered = dashboard.jinja_env.from_string(
            "{{ media_url('/data/assets/a.jpg') }}").render()
    assert rendered.endswith("/assets/a.jpg")


def test_a_download_never_goes_to_blob(dashboard, container):
    """A blob URL cannot set Content-Disposition without a SAS token, and that
    header is the only way to save a video out of iOS Safari."""
    with dashboard.test_request_context("/"):
        rendered = dashboard.jinja_env.from_string(
            "{{ media_url('/data/assets/a.mp4', download=True) }}").render()
    assert "blob.core.windows.net" not in rendered
    assert rendered.endswith("/assets/a.mp4?download=1")


def test_no_template_bypasses_the_helper():
    """A new <img src="{{ url_for('asset' ...) }}" would quietly route media
    back through the VM."""
    from pathlib import Path as _P

    templates = _P("aismm/dashboard/templates")
    offenders = [p.name for p in templates.glob("*.html") if "url_for('asset'" in p.read_text()]
    assert offenders == []
