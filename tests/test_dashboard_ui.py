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


def test_a_run_shows_what_it_produced(dash, store, account):
    """On the RUNS list, where each row has its own asset — the fastest way to
    spot a run that went wrong without opening it."""
    instruction = _instruction(store, "Comicbook")
    store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                      status=RunStatus.published, asset_path="/x/panel.jpg"))
    page = dash.test_client().get("/runs").get_data(as_text=True)
    assert 'class="thumb"' in page
    assert "panel.jpg" in page


def test_a_run_with_no_media_still_renders(dash, store, account):
    instruction = _instruction(store, "Comicbook")
    store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                      status=RunStatus.failed))
    page = dash.test_client().get("/runs").get_data(as_text=True)
    assert "thumb-empty" in page


def test_the_instruction_list_has_no_thumbnail(dash, store, account):
    """It belonged on runs: an instruction has no media of its own."""
    instruction = _instruction(store, "Comicbook")
    store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                      status=RunStatus.published, asset_path="/x/panel.jpg"))
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert 'class="thumb"' not in page


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


# --- brand ---------------------------------------------------------------------------- #
# The AISM² mark from the design system. The A is a PATH, not text: a favicon
# rendered with a font falls back to whatever the viewer has installed, so the
# mark would differ per machine.

BRAND = __import__("pathlib").Path(__file__).resolve().parents[1] / "aismm/dashboard/static"


@pytest.mark.parametrize("name", [
    "brand/icon.svg", "brand/avatar.svg", "brand/mark-dark.svg", "brand/mark-light.svg",
    "brand/mark-dark-sm.svg", "brand/logo.svg",
    "favicon.ico", "favicon-32.png", "apple-touch-icon.png",
])
def test_every_brand_asset_exists(name):
    assert (BRAND / name).is_file()


@pytest.mark.parametrize("name", ["brand/icon.svg", "brand/mark-dark.svg", "brand/logo.svg"])
def test_the_letterform_is_a_path_not_a_font(name):
    svg = (BRAND / name).read_text()
    assert "M50 8 L84 92" in svg                  # the A outline
    assert "M50 38 L55.33 62" in svg              # ...and its counter
    assert "fill-rule=\"evenodd\"" in svg         # which makes the counter a hole


@pytest.mark.parametrize("name", ["brand/icon.svg", "brand/mark-dark.svg", "brand/logo.svg"])
def test_the_palette_is_the_design_system_one(name):
    svg = (BRAND / name).read_text()
    assert "#E85C7A" in svg                       # accent
    assert "#1c1e27" in svg                       # ink


def test_the_small_mark_drops_the_squared_motif():
    """The design drops the "2" below ~48px, where it would be a smudge."""
    assert "<text" not in (BRAND / "brand/mark-dark-sm.svg").read_text()
    assert "<text" not in (BRAND / "brand/icon.svg").read_text()


def test_the_large_mark_keeps_it():
    assert ">2<" in (BRAND / "brand/mark-dark.svg").read_text()


def test_the_wordmark_cannot_overflow_its_card():
    """Laid out by hand it did: a fallback font of a different width pushed the
    type out through the side. textLength pins it."""
    svg = (BRAND / "brand/logo.svg").read_text()
    assert svg.count("textLength=") == 2
    assert 'lengthAdjust="spacingAndGlyphs"' in svg


def test_the_page_declares_every_favicon_form(dash):
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert 'type="image/svg+xml"' in page         # modern browsers
    assert "favicon.ico" in page                  # everything else
    assert "apple-touch-icon" in page             # iOS home screen
    assert 'name="theme-color" content="#1c1e27"' in page


def test_the_header_carries_the_mark_and_wordmark(dash):
    page = dash.test_client().get("/instructions").get_data(as_text=True)
    assert "brand/mark-dark-sm.svg" in page
    assert "AISM<sup>2</sup>" in page
    assert "🤖" not in page                        # the placeholder emoji is gone


def test_the_brand_tokens_are_defined():
    css = (BRAND / "style.css").read_text()
    for token in ("--brand-ink: #1c1e27", "--brand-paper: #faf9f6",
                  "--brand-accent: #E85C7A"):
        assert token in css


def _css_var(name):
    css = (BRAND / "style.css").read_text()
    return css.split(f"{name}: ")[1].split(";")[0].strip()


def test_the_brand_accent_is_not_the_ui_accent():
    """Reported: the rose read as the Delete colour. It is 16 units from
    --danger in RGB — the same colour, to a human — so a primary button and a
    destructive one became nearly indistinguishable."""
    assert _css_var("--brand-accent") == "#E85C7A"
    assert _css_var("--accent") == "#6ea8fe"


def test_the_ui_accent_is_nowhere_near_the_danger_colour():
    """The property that actually matters, checked rather than assumed."""
    def _rgb(value):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))

    def _distance(a, b):
        return sum((x - y) ** 2 for x, y in zip(_rgb(a), _rgb(b))) ** 0.5

    accent, danger = _css_var("--accent"), _css_var("--danger")
    assert _distance(accent, danger) > 100, f"{accent} is too close to {danger}"


def test_the_brand_accent_appears_only_on_the_mark():
    """The one place that colour is allowed in the UI is the squared motif."""
    css = (BRAND / "style.css").read_text()
    uses = [line.strip() for line in css.splitlines()
            if "var(--brand-accent)" in line and not line.strip().startswith(("/*", "*"))]
    assert len(uses) == 1
    assert ".brand-word sup" in uses[0]


def test_the_accent_is_readable_on_the_dark_ui():
    """Whatever the accent is, links have to be legible against the panels."""
    def _luminance(value):
        channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                  for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    accent = _css_var("--accent")
    for background in ("#0f1115", "#171a21"):
        lighter, darker = sorted((_luminance(accent), _luminance(background)), reverse=True)
        assert (lighter + 0.05) / (darker + 0.05) >= 4.5, f"{accent} on {background}"


# --- the mark and the wordmark are alternatives, not a pair ---------------------------- #

def test_the_header_shows_the_wordmark_by_default():
    """The mark IS an A: beside "AISM²" it reads as a stray letter."""
    css = (BRAND / "style.css").read_text()
    assert ".brand-mark { display: none;" in css


def test_the_mark_replaces_the_wordmark_only_when_space_runs_out():
    css = (BRAND / "style.css").read_text()
    # Everything after the media query's opening brace, to its closing one.
    narrow = css.split("@media (max-width: 420px) {")[1]
    narrow = narrow[:narrow.index("\n}")]
    assert ".brand-mark { display: block; }" in narrow
    assert ".brand-word { display: none; }" in narrow


def test_the_login_page_uses_the_wordmark_alone():
    login = (BRAND.parents[0] / "templates/login.html").read_text()
    assert "AISM<sup>2</sup>" in login
    assert "brand-mark" not in login


def test_the_rasters_can_be_regenerated():
    """One definition of the mark, not a binary someone hand-edited."""
    script = (BRAND.parents[2] / "scripts/make_brand_assets.py").read_text()
    assert "M50 8" not in script or "A_OUTER" in script
    assert "(50, 8)" in script                    # the same geometry as the SVGs


# --- the overview dashboard ------------------------------------------------- #

def test_overview_empty_state_onboards(dash, store):
    """A fresh sign-in has no accounts, so the overview leads with onboarding
    instead of empty charts."""
    page = dash.test_client().get("/").get_data(as_text=True)
    assert "Welcome to AISM" in page
    assert "Connect an account" in page
    assert "onboard-step" in page
    # No charts to draw without data.
    assert 'class="chart"' not in page


def test_overview_with_accounts_shows_insights(dash, store, account):
    """Once connected it becomes a real dashboard: KPIs, an activity chart and a
    recent-activity feed built from actual runs."""
    instruction = _instruction(store, "Comicbook")
    store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                      status=RunStatus.published, asset_path="/x/panel.jpg",
                      caption="hello world"))
    store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                      status=RunStatus.failed, error="boom"))
    page = dash.test_client().get("/").get_data(as_text=True)
    assert "Connected accounts" in page
    assert 'class="chart"' in page              # activity chart is drawn
    assert "Recent activity" in page
    assert "Comicbook" in page                  # the run's instruction
    assert "Welcome to AISM" not in page        # not the onboarding state


def test_overview_metrics_count_only_real_outcomes(dash, store, account):
    """Success rate is published / (published + failed) — the honest denominator,
    not a fabricated reach figure."""
    instruction = _instruction(store, "News")
    for _ in range(3):
        store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                          status=RunStatus.published))
    store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                      status=RunStatus.failed))
    insights = app_module._overview_insights(store, "")
    assert insights["published_window"] == 3
    assert insights["totals"]["failed"] == 1
    assert insights["success_rate"] == 75        # 3 / (3 + 1)
