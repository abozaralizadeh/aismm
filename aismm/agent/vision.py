"""Letting the agent LOOK at an image.

``browse_page`` returns an image's URL, alt text and caption — everything
*around* the picture, never the picture. That is enough to pick a comic panel
out of a numbered list, and not nearly enough to answer "which of these four
frames shows the character holding the letter?" or "does this screenshot
actually contain a chart?". The agent had to either guess or give up.

Why here and not in the tool: tools do deterministic work only (repo
convention), and an LLM call belongs in the agent layer — the same reasoning
that put memory compaction in :mod:`aismm.agent.memory`. The tool in
:mod:`aismm.tools.vision_tool` does the deterministic half (resolve the target,
fetch the bytes, check they are an image) and calls in here for the one step
that needs a model.

This is a **small, separate** agent, not the manager: describing a picture must
not inherit the manager's instructions, memory or tools, and it must not be
able to publish anything.
"""
from __future__ import annotations

import base64
import logging

from agents import Agent, Runner

from ..llm import STATELESS_RUN_CONFIG, agent_model_settings, build_model

logger = logging.getLogger("aismm.agent.vision")

DESCRIBER_INSTRUCTIONS = """\
You describe images for another AI agent that cannot see them. That agent will
act on what you say, so accuracy matters more than style.

Report only what is actually visible. Cover, when present:
- the subject and what is happening
- any TEXT in the image, transcribed exactly (speech bubbles, captions, labels,
  signage, UI text, watermarks) — this is usually the most useful part
- people: how many, what they are doing, their expressions; describe appearance
  only as far as the question needs, and never guess identity, ethnicity, age or
  emotional state as fact
- layout, panel order and reading order for comics, diagrams or screenshots
- image quality problems worth knowing about: blur, heavy compression, a
  watermark, a crop that cuts off content, an obvious placeholder or error page

If the question cannot be answered from the image, say so plainly instead of
inferring. Never invent detail to be helpful — a confident wrong description is
worse than "not visible in this image".

Answer in prose, no preamble. Be thorough but stay under 400 words unless the
question asks for a full transcription.
"""

DEFAULT_QUESTION = "Describe this image."

# Long enough for a dense page of text, short enough to stay a cheap look.
_MAX_OUTPUT_TOKENS = 1200


def _agent(model=None) -> Agent:
    return Agent(
        name="Image describer",
        instructions=DESCRIBER_INSTRUCTIONS,
        model=model or build_model(),
        model_settings=agent_model_settings(max_tokens=_MAX_OUTPUT_TOKENS),
    )


async def describe_image(data: bytes, *, mime: str = "image/jpeg", question: str = "",
                         source: str = "", model=None) -> str:
    """Return a description of ``data``, answering ``question`` if one is given.

    ``model`` reuses the run's connection when the caller has one on
    ``state["model"]``; it falls back to the deployment default otherwise.

    Raises on failure; the caller turns that into a tool-shaped error so the
    agent can decide whether to carry on without having seen the picture.
    """
    encoded = base64.b64encode(data).decode()
    prompt = (question or "").strip() or DEFAULT_QUESTION
    parts = [
        {"type": "input_text", "text": prompt},
        # "high" detail, deliberately: the questions worth asking are about small
        # things — the text in a speech bubble, which panel a character appears
        # in — and "auto" downsamples exactly those away.
        {"type": "input_image",
         "image_url": f"data:{mime};base64,{encoded}", "detail": "high"},
    ]
    logger.info("Describing %s (%d bytes, %s) — %r",
                source or "an image", len(data), mime, prompt[:120])
    result = await Runner.run(_agent(model), input=[{"role": "user", "content": parts}],
                              run_config=STATELESS_RUN_CONFIG)
    return (result.final_output or "").strip()
