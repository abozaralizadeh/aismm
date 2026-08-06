"""Dashboard usability: finding things, judging media, and acting on it.

Six reported problems, each with a test here:
  1. the instruction list had no search, filter or sort, and no way to see what
     an instruction has been producing;
  2. staged media was oversized, and a single item sat in a narrow column with
     empty space beside it;
  3. video was CROPPED in the preview — you were reviewing a post while unable
     to see its edges;
  4. a staged post could not be approved or rejected from the run page, which is
     the only page that shows it;
  5. no way to save media on a phone (iOS Safari cannot save a playing video);
  6. the accounts page hid each connection's settings in a colspan row beneath
     the table row it belonged to.
"""
import dataclasses

import pytest

from aismm import config as config_module
from aismm.config import AuthSettings
from aismm.dashboard import app as app_module
from aismm.dashboard import sso
from aismm.models import (
    Account, Instruction, PlatformName, PublishMode, Run, RunStatus, StagedPost, StagedStatus,
)

CSS = (__import__("pathlib").Path(__file__).resolve().parents[1]
       / "aismm/dashboard/static/style.css").read_text()


@pytest.fixture()
def dash(store, monkeypatch, tmp_path):
    (tmp_path / "assets").mkdir(exist_ok=True)
    patched = dataclasses.replace(config_module.settings, auth=AuthSettings(),
                                  data_dir=tmp_path)
    from aismm import assets as assets_module

    for module in (sso, app_module, config_module, assets_module):
        monkeypatch.setattr(module, "settings", patched)
    monkeypatch.setattr(app_module, "get_store", lambda: store)
    application = app_module.create_app()
    application.secret_key = "test"
    return application


@pytest.fixture()
def account(store):
    return store.upsert_account(
        Account(platform=PlatformName.twitter, handle="abo0zar", external_id="9"),
        access_token="t")


def _instruction(store, name, **kwargs):
    return store.upsert_instruction(Instruction(name=name, **kwargs))


# --- 1. the instruction list ---------------------------------------------------------- #

def test_instructions_can_be_searched(dash, store):
    _instruction(store, "Comicbook")
    _instruction(store, "Daily news")
    page = dash.test_client().get("/instructions?q=news").get_data(as_text=True)
    assert "Daily news" in page
    assert "Comicbook" not in page


def test_the_search_covers_the_brief_and_the_schedule(dash, store):
    _instruction(store, "Alpha", brief="one panel a day")
    _instruction(store, "Beta", schedule="every 6h")
    client = dash.test_client()
    assert "Alpha" in client.get("/instructions?q=panel").get_data(as_text=True)
    assert "Beta" in client.get("/instructions?q=6h").get_data(as_text=True)


def test_instructions_can_be_filtered_by_state_and_mode(dash, store):
    _instruction(store, "Live one", enabled=True, publish_mode=PublishMode.live)
    _instruction(store, "Paused one", enabled=False, publish_mode=PublishMode.dry_run)
    client = dash.test_client()
    enabled = client.get("/instructions?enabled=1").get_data(as_text=True)
    assert "Live one" in enabled and "Paused one" not in enabled
    by_mode = client.get("/instructions?mode=dry_run").get_data(as_text=True)
    assert "Paused one" in by_mode and "Live one" not in by_mode


def test_instructions_can_be_sorted(dash, store):
    _instruction(store, "Zebra")
    _instruction(store, "Apple")
    page = dash.test_client().get("/instructions?sort=name&dir=asc").get_data(as_text=True)
    assert page.index("Apple") < page.index("Zebra")
    page = dash.test_client().get("/instructions?sort=name&dir=desc").get_data(as_text=True)
    assert page.index("Zebra") < page.index("Apple")


def test_an_empty_filter_result_offers_a_way_back(dash, store):
    _instruction(store, "Comicbook")
    page = dash.test_client().get("/instructions?q=zzzz").get_data(as_text=True)
    assert "Nothing matches that filter" in page
    assert "No instructions yet" not in page          # not the same situation


def test_the_newest_media_is_shown_as_a_thumbnail(dash, store, account, tmp_path):
    """The fastest way to spot an instruction that has quietly gone wrong."""
    instruction = _instruction(store, "Comicbook")
    (tmp_path / "assets" / "panel.jpg").write_bytes(b"\xff\xd8\xff")
    store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                      status=RunStatus.published, asset_path="/x/panel.jpg"))
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert 'class="thumb"' in page
    assert "panel.jpg" in page


def test_an_instruction_with_no_media_still_renders(dash, store):
    _instruction(store, "Fresh")
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert "thumb-empty" in page


# --- 2 & 3. reviewing staged media ---------------------------------------------------- #

def test_preview_media_is_contained_not_cropped():
    """A 9:16 reel in a 4:3 box was being cropped — you cannot review the edges
    of a post you cannot see."""
    staged = CSS.split(".staged-media video")[1].split("}")[0]
    assert "object-fit: contain" in staged
    assert "object-fit: cover" not in staged


def test_the_run_page_media_is_contained_too():
    detail = CSS.split(".detail-media video")[1].split("}")[0]
    assert "object-fit: contain" in detail


def test_a_thumbnail_may_still_crop():
    """A thumbnail is a glance, not the thing being judged."""
    thumb = CSS.split(".thumb {")[1].split("}")[0]
    assert "object-fit: cover" in thumb


def test_the_staged_list_scrolls_instead_of_growing():
    block = CSS.split(".staged-list {")[1].split("}")[0]
    assert "overflow-y: auto" in block
    assert "max-height" in block


def test_a_single_staged_card_does_not_sit_in_a_narrow_column():
    """auto-fill leaves empty tracks beside one card; auto-fit collapses them,
    and the max track width stops that card stretching across a wide screen."""
    block = CSS.split(".staged-list {")[1].split("}")[0]
    assert "auto-fit" in block
    assert "auto-fill" not in block
    assert "380px" in block


def test_videos_play_inline_on_ios(dash, store, account):
    """Without playsinline, iOS Safari takes over the whole screen to play."""
    instruction = _instruction(store, "Comicbook")
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            status=RunStatus.staged, asset_path="/x/reel.mp4"))
    page = dash.test_client().get(f"/runs/{run.id}").get_data(as_text=True)
    assert "playsinline" in page


# --- 4. deciding a staged post where you can see it ------------------------------------ #

def _pending(store, account, instruction):
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            status=RunStatus.staged, caption="c", asset_path="/x/a.jpg"))
    staged = store.add_staged(StagedPost(instruction_id=instruction.id, account_id=account.id,
                                         run_id=run.id, caption="c", media_kind="image",
                                         asset_path="/x/a.jpg",
                                         status=StagedStatus.pending_approval))
    return run, staged


def test_a_pending_post_can_be_approved_from_the_run_page(dash, store, account):
    instruction = _instruction(store, "Comicbook")
    run, _staged = _pending(store, account, instruction)
    page = dash.test_client().get(f"/runs/{run.id}").get_data(as_text=True)
    assert "Approve &amp; publish" in page
    assert "Reject" in page


def test_a_settled_post_offers_no_decision(dash, store, account):
    """Only pending ones — an already-published post has nothing to approve."""
    instruction = _instruction(store, "Comicbook")
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            status=RunStatus.published))
    store.add_staged(StagedPost(instruction_id=instruction.id, account_id=account.id,
                                run_id=run.id, status=StagedStatus.published,
                                external_url="https://x.com/p/1"))
    page = dash.test_client().get(f"/runs/{run.id}").get_data(as_text=True)
    assert "Approve &amp; publish" not in page
    assert "open ↗" in page


def test_rejecting_returns_to_the_page_you_were_on(dash, store, account):
    instruction = _instruction(store, "Comicbook")
    run, staged = _pending(store, account, instruction)
    response = dash.test_client().post(f"/staged/{staged.id}/reject",
                                       headers={"Referer": f"/runs/{run.id}"})
    assert response.headers["Location"].endswith(f"/runs/{run.id}")


# --- 5. getting the file onto a phone -------------------------------------------------- #

def test_media_can_be_downloaded(dash, tmp_path):
    (tmp_path / "assets" / "reel.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    response = dash.test_client().get("/assets/reel.mp4?download=1")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]


def test_media_is_still_served_inline_by_default(dash, tmp_path):
    """Instagram fetches this URL server-side; it must not become an attachment."""
    (tmp_path / "assets" / "reel.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    response = dash.test_client().get("/assets/reel.mp4")
    assert "attachment" not in (response.headers.get("Content-Disposition") or "")


def test_the_run_page_offers_download_and_open(dash, store, account):
    instruction = _instruction(store, "Comicbook")
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            status=RunStatus.published, asset_path="/x/reel.mp4"))
    page = dash.test_client().get(f"/runs/{run.id}").get_data(as_text=True)
    assert "download=1" in page
    assert "Open in a new tab" in page


# --- 6. the accounts page -------------------------------------------------------------- #

def test_each_account_is_its_own_card(dash, store, account):
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "account-card" in page
    # ...not a table with the settings orphaned in a colspan row beneath it.
    assert "community-row" not in page


def test_the_card_carries_the_token_state_and_the_actions(dash, store, account):
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert "Check permissions" in page
    assert "Disconnect" in page


def test_the_x_destination_lives_on_the_x_card_only(dash, store, account):
    store.upsert_account(Account(platform=PlatformName.instagram, handle="ig",
                                 external_id="1"), access_token="t")
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert page.count('name="community_id"') == 1


def test_connecting_comes_after_what_is_already_connected(dash, store, account):
    """The common visit is to check an existing account, not to add one."""
    page = dash.test_client().get("/accounts").get_data(as_text=True)
    assert page.index("Connected") < page.index("Connect another")


# --- the list must fit, and must show what a schedule MEANS ---------------------------- #

def test_table_pages_get_a_wider_column():
    """960px clipped the last column and forced a horizontal scroll on a desktop
    with room to spare."""
    assert "main.wide" in CSS
    for template in ("instructions.html", "runs.html"):
        page = (__import__("pathlib").Path(__file__).resolve().parents[1]
                / "aismm/dashboard/templates" / template).read_text()
        assert "{% block main_class %}wide{% endblock %}" in page


def test_a_cron_string_is_not_wrapped_mid_expression():
    """"0 6 * *" / "*,0 16 *" across three lines is unreadable."""
    block = CSS.split(".schedule-raw {")[1].split("}")[0]
    assert "nowrap" in block
    assert "ellipsis" in block


def test_the_schedule_is_shown_in_plain_english(dash, store):
    _instruction(store, "Comicbook", schedule="0 16 * * *")
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert "at 16:00 UTC" in page


def test_a_schedule_that_never_fires_is_flagged(dash, store):
    """Silent failure: the row looks configured and the instruction has simply
    never run. Two raw crons joined by a comma do exactly this."""
    _instruction(store, "Broken", schedule="0 6 * * *,0 16 * * *")
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert "never fires" in page
    assert "badge-failed" in page


def test_a_working_schedule_is_not_flagged(dash, store):
    _instruction(store, "Fine", schedule="09:00")
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert "never fires" not in page
