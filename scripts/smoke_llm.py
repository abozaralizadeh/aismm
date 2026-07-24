"""Smoke test: verify the LLM wiring (Azure OpenAI or APIM) end to end.

    python scripts/smoke_llm.py

Builds the shared model via whatever LLM_PROVIDER is set, runs a trivial agent
turn, and prints the reply. A clean run proves the provider toggle + client are
configured correctly. Does not require any social credentials.
"""
import asyncio

from agents import Agent, Runner

from aismm.llm import build_model


async def main() -> None:
    agent = Agent(name="Smoke", instructions="You are terse.", model=build_model())
    result = await Runner.run(agent, "Reply with exactly: LLM wiring OK", max_turns=2)
    print("MODEL SAID:", result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
