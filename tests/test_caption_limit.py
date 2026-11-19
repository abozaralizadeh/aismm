"""A caption never reaches a platform over its hard limit.

On 2026-11-16 Instagram refused the Comicbook carousel five runs in a row with
"Caption Too Long" (2,252 to 2,311 characters against 2,200). The kickoff stated
the limit, but the only code that enforced it lived inside the AI disclosure,
which is off by default.
"""
import asyncio

import pytest

from aismm import captions
from aismm.models import Account, Instruction, PlatformName, PublishMode, Run, RunStatus
from aismm.platforms.base import Capabilities, PublishResult
from aismm.tools.publish_tool import perform_publish

BODY = ("Panel four. The broadcast cuts out mid-sentence and the room goes still. "
        "Nobody moves. ") * 30
TAGS = " ".join(f"#tag{i}" for i in range(20))


# --- counting and trimming ----------------------------------------------------------- #

def test_an_emoji_counts_as_two():
    assert captions.units("abc") == 3
    assert captions.units("🙂") == 2


def test_a_caption_that_fits_is_untouched():
    assert captions.fit("short caption #a #b", 2200) == ("short caption #a #b", "")


def test_trailing_hashtags_go_first():
    text = "x" * 2150 + "\n\n" + TAGS
    fitted, note = captions.fit(text, 2200)
    assert captions.units(fitted) <= 2200
    assert fitted.startswith("x" * 2150)            # the body is whole
    assert "#tag0" in fitted and "#tag19" not in fitted
    assert "hashtag" in note


def test_a_long_body_is_cut_at_a_sentence_and_keeps_a_few_tags():
    fitted, note = captions.fit(BODY + "\n\n" + TAGS, 2200)
    assert captions.units(fitted) <= 2200
    body, _, tags = fitted.rpartition("\n\n")
    assert body.endswith(".")                       # a whole sentence, no mid-word cut
    assert tags.split() == [f"#tag{i}" for i in range(captions.KEEP_TAGS_AFTER_CUT)]
    assert "clean break" in note


def test_no_break_falls_back_to_a_word_with_an_ellipsis():
    fitted, _ = captions.fit("word " * 1000, 2200)
    assert captions.units(fitted) <= 2200 and fitted.endswith("…")
    assert not fitted[:-1].endswith(" ")


def test_emoji_heavy_text_fits_in_units_not_characters():
    fitted, _ = captions.fit("🙂" * 1500, 2200)
    assert captions.units(fitted) <= 2200


# --- the publish path ------------------------------------------------------------------ #

@pytest.fixture()
def live(store, monkeypatch):
    sent = {}

    class FakeInstagram:
        capabilities = Capabilities(True, True, True, False, "portrait", 2200)

        def capabilities_for(self, account):
            return self.capabilities

        async def publish(self, *, access_token, account, caption, asset_path, media_kind,
                          instruction=None, asset_paths=None, placement="feed"):
            sent["caption"] = caption
            return PublishResult(url="https://instagram.com/p/1")

    import aismm.platforms.registry as reg

    monkeypatch.setattr(reg, "get_platform", lambda name: FakeInstagram())
    acct = store.upsert_account(Account(platform=PlatformName.instagram, handle="c"),
                                access_token="tok")
    instr = store.upsert_instruction(Instruction(name="i", publish_mode=PublishMode.live))
    run = store.add_run(Run(instruction_id=instr.id, account_id=acct.id,
                            status=RunStatus.running))
    state = {"account": acct, "instruction": instr, "store": store, "run": run, "assets": []}
    return state, sent


def test_the_agent_is_asked_to_shorten_once(live):
    state, sent = live
    long = BODY + TAGS
    first = asyncio.run(perform_publish(state, long, "", "text", shorten_first=True))
    assert first["error"] == "caption_too_long"
    assert "2200" in first["message"] and str(captions.aim(2200)) in first["message"]
    assert "caption" not in sent                     # nothing went out

    second = asyncio.run(perform_publish(state, long, "", "text", shorten_first=True))
    assert second["status"] == "published"
    assert captions.units(sent["caption"]) <= 2200
    assert "CAPTION TRIMMED" in state["run"].log


def test_a_republish_is_trimmed_without_asking(live):
    state, sent = live
    result = asyncio.run(perform_publish(state, BODY + TAGS, "", "text"))
    assert result["status"] == "published"
    assert captions.units(sent["caption"]) <= 2200


def test_a_caption_under_the_limit_goes_out_as_written(live):
    state, sent = live
    asyncio.run(perform_publish(state, "A short caption. #comic", "", "text",
                                shorten_first=True))
    assert sent["caption"] == "A short caption. #comic"


def test_the_kickoff_asks_for_well_under_the_cap():
    from aismm.agent import prompts

    caps = Capabilities(True, True, True, False, "portrait", 2200)
    kickoff = prompts.build_kickoff(account=Account(platform=PlatformName.instagram),
                                    instruction=Instruction(name="I", brief="b"),
                                    platform_caps=caps)
    assert "caption limit: 2200 characters, a HARD cap" in kickoff
    assert f"at most {captions.aim(2200)}" in kickoff


def test_a_threading_platform_gets_no_aim():
    from aismm.agent.prompts import _caption_aim

    caps = Capabilities(True, True, True, False, "landscape", 280, supports_threads=True)
    assert _caption_aim(caps) == ""
