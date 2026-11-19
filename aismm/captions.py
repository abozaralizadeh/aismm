"""Make a caption fit a platform's hard character limit, cleanly.

Instagram refuses a caption over 2,200 characters outright ("Caption Too Long",
code 36004 / subcode 2207010), and on 2026-11-16 the Comicbook instruction hit it
five runs in a row with captions of 2,252 to 2,311 characters. The kickoff did
state the limit, and the only code that enforced it lived inside the AI
disclosure, which is off by default, so nothing stood between the model's
arithmetic and the API.

Length is counted in UTF-16 code units, which is never less than the number of
code points: an emoji counts as two. That over-counts a little for platforms
that count code points, which only ever trims a caption that would have fitted
anyway. Under-counting is the failure that reaches the API.

The trim keeps what a reader needs. A trailing block of hashtags goes first,
one tag at a time from the end; if that is not enough, the body is cut at the
last paragraph or sentence break that fits, and a few hashtags are kept after
it. Only when there is no such break in the back half is it cut at a word, with
an ellipsis.
"""
from __future__ import annotations

import re

#: How many hashtags survive when the body itself had to be cut.
KEEP_TAGS_AFTER_CUT = 5
#: A boundary earlier than this share of the room is not worth stopping at.
_MIN_KEEP = 0.5
_ELLIPSIS = "…"
_SENTENCE_END = re.compile(r"[.!?…؟。](?=[\s\"'”’)\]]|$)")


def units(text: str) -> int:
    """Length as UTF-16 code units: what the strictest platform counts."""
    return len((text or "").encode("utf-16-le")) // 2


def _prefix(text: str, room: int) -> str:
    """The longest prefix of ``text`` that is at most ``room`` units long."""
    if units(text) <= room:
        return text
    total = 0
    for index, char in enumerate(text):
        total += 2 if ord(char) > 0xFFFF else 1
        if total > room:
            return text[:index]
    return text


def _split_tags(text: str) -> tuple[str, list[str]]:
    """Split off a TRAILING run of hashtags (the usual end of an Instagram caption)."""
    tokens = text.split()
    tags: list[str] = []
    while tokens and tokens[-1].startswith("#") and len(tokens[-1]) > 1:
        tags.insert(0, tokens.pop())
    if not tags:
        return text, []
    # Cut the original string, so the body keeps its own line breaks.
    position = len(text)
    for tag in reversed(tags):
        position = text.rfind(tag, 0, position)
    return text[:position].rstrip(), tags


def _join(body: str, tags: list[str]) -> str:
    return f"{body}\n\n{' '.join(tags)}" if body and tags else (body or " ".join(tags))


def _cut_body(body: str, room: int) -> str:
    """Cut ``body`` to ``room`` units at the cleanest boundary available."""
    head = _prefix(body, room)
    if head == body:
        return body
    floor = int(room * _MIN_KEEP)
    for marker in ("\n\n", "\n"):
        at = head.rfind(marker)
        if at >= floor:
            return head[:at].rstrip()
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= floor:
        return head[:ends[-1]].rstrip()
    head = _prefix(body, room - units(_ELLIPSIS))
    at = head.rstrip().rfind(" ")
    if at >= floor:
        head = head[:at]
    return head.rstrip(" ,;:") + _ELLIPSIS


def fit(caption: str, limit: int) -> tuple[str, str]:
    """``(caption that fits, note)``. The note is empty when nothing had to change."""
    text = (caption or "").strip()
    if not limit or limit <= 0 or units(text) <= limit:
        return text, ""
    original = units(text)
    body, tags = _split_tags(text)

    kept = list(tags)
    while kept and units(_join(body, kept)) > limit:
        kept.pop()
    if units(_join(body, kept)) <= limit and units(body) <= limit:
        dropped = len(tags) - len(kept)
        return _join(body, kept), (f"caption was {original} characters, over the {limit} "
                                   f"limit; dropped {dropped} trailing hashtag(s)")

    kept = tags[:KEEP_TAGS_AFTER_CUT]
    room = limit - (units(_join("x", kept)) - 1 if kept else 0)
    if room < limit * _MIN_KEEP:
        kept, room = [], limit
    result = _join(_cut_body(body, room), kept)
    return result, (f"caption was {original} characters, over the {limit} limit; cut to "
                    f"{units(result)} at a clean break"
                    + (f", keeping {len(kept)} hashtag(s)" if kept else ""))


def aim(limit: int) -> int:
    """What to ASK the model for: well under the cap, since its counting is approximate."""
    return int(limit * 0.85)
