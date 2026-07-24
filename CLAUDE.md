# CLAUDE.md

Guidance for Claude Code when working in this repo. For end-user setup (creating developer
apps, connecting accounts, env vars) see [README.md](README.md) — don't duplicate it here.

## What this is

**AI Social Media Manager (AISMM)** — an autonomous, agent-driven framework that publishes content
to Instagram, X (Twitter), YouTube, and TikTok. An [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
agent researches a topic, generates media (Sora 2 video / gpt-image-1), writes a caption, and
publishes on a schedule. Python 3.10+, standalone repo (not part of SandBox, though it borrows its
patterns).

## Commands

```bash
# setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in Azure OpenAI (or APIM) at minimum

# run
python -m aismm.cli run         # scheduler + dashboard  → http://127.0.0.1:8787
python -m aismm.cli dashboard   # dashboard only
python -m aismm.cli list        # accounts + instructions
python -m aismm.cli post --instruction <id-or-name> [--account <id>]

# test / verify
pytest -q                       # 18 unit tests (no network, no creds needed)
python scripts/smoke_llm.py     # verifies Azure/APIM LLM wiring (needs LLM creds)
python scripts/smoke_sora.py    # generates one Sora clip (skips if unconfigured)
```

There is no lint/format config; match the surrounding style (stdlib logging, `from __future__ import
annotations`, type hints, ~100-col lines).

## Architecture & data flow

`Instruction` (dashboard-authored: brief + accounts + schedule + publish_mode) → **APScheduler**
([scheduler.py](aismm/scheduler.py)) fires → [orchestrator.py](aismm/orchestrator.py)
`run_instruction()` loops selected accounts, takes a **single-flight lock**, creates a `Run`, and
calls the agent → [agent/manager_agent.py](aismm/agent/manager_agent.py) builds an `Agent` with
per-run tools and does `Runner.run` + deterministic recovery → the agent finishes by calling the
**`publish`** tool, which **gates on `instruction.publish_mode`** in code.

The publish gate is the core design point (autonomy + guardrail): the agent always "publishes", but
`perform_publish` in [tools/publish_tool.py](aismm/tools/publish_tool.py) does:
`dry_run` → StagedPost(preview) · `approval` → StagedPost(pending) → dashboard Approve → platform API
· `live` → platform API now. Keep this gate intact when editing publish logic.

## Conventions (follow these)

- **Tools are `state`-closure factories.** A tool factory is `fn(state: dict) -> Tool | None`,
  registered via `register_tool(name, factory)` ([tools/registry.py](aismm/tools/registry.py)). Each
  builds an inner `@function_tool async def` that closes over the per-run `state`
  (`{account, instruction, store, run, assets, result}`). Return `None` to disable a tool for a run
  (e.g. Sora when unconfigured). **No LLM calls inside a tool** — tools do deterministic work only;
  the tool docstring is what the model sees.
- **Platforms subclass `SocialPlatform`** ([platforms/base.py](aismm/platforms/base.py)): declare
  OAuth endpoints/scopes + `Capabilities` as class attrs, implement `fetch_identity` + `publish`,
  then `register(PlatformName.x, Cls)`. Generic OAuth (authorize URL / code exchange / refresh) is
  inherited; override only when a platform differs (TikTok uses `client_key`, so it overrides them).
- **Storage goes through the `Store` interface** ([store/base.py](aismm/store/base.py)). Default is
  `LocalStore` (SQLite + SQLModel). Never read/write the DB directly from routes/agent code — call
  the store. Tokens cross this boundary in plaintext; the store encrypts (Fernet) internally.
- **Config is a frozen `Settings` singleton** ([config.py](aismm/config.py)) read from env **at
  import time**. Tests that need different config set env before import or pass an explicit `db_url`
  to `LocalStore`. Don't call `os.getenv` elsewhere.
- **Extension points, not edits:** add a network or capability by registering a new class/factory in
  its own file (+ import it in the package `__init__`), rather than modifying the agent.

## Key files

| Path | Role |
|---|---|
| [aismm/config.py](aismm/config.py) | env → typed `Settings` (LLM toggle, Sora pool, paths, platform creds) |
| [aismm/llm.py](aismm/llm.py) | `build_model()` — Azure-direct **or** APIM client → `OpenAIResponsesModel` |
| [aismm/agent/manager_agent.py](aismm/agent/manager_agent.py) | agent build + `Runner.run` + recovery |
| [aismm/agent/prompts.py](aismm/agent/prompts.py) | inline system prompt + kickoff builder |
| [aismm/tools/](aismm/tools/) | registry + web_search, sora_client/config, video/image/publish/context tools |
| [aismm/platforms/](aismm/platforms/) | base + registry + instagram/twitter/youtube/tiktok |
| [aismm/orchestrator.py](aismm/orchestrator.py) | per-account run + lock + `approve_staged`/`reject_staged` |
| [aismm/store/](aismm/store/) | base + local_store (SQLite) + azure_store (adapter stub) |
| [aismm/dashboard/app.py](aismm/dashboard/app.py) | Flask control center (accounts, instructions, runs, OAuth callbacks, `/assets`) |
| [aismm/models.py](aismm/models.py) | SQLModel tables + `PublishMode`/`PlatformName`/`RunStatus`/… enums |

## Gotchas

- **Sora 2** ([tools/sora_client.py](aismm/tools/sora_client.py)): job-scoped — a job id only exists
  on the resource that created it, so create/poll/download must stay on one resource. The pool
  round-robins **at the job level**; never front it with a round-robin gateway. Sora 2 has **no
  seed**; `input_reference` rejects human faces. The Videos API is announced for shutdown ~Sep 24
  2026 — the tool is behind the registry so a successor can replace it.
- **Instagram needs a PUBLIC media URL** — it fetches media, no binary upload. Assets are served at
  `DASHBOARD_BASE_URL/assets/<file>`; the IG integration raises if that resolves to localhost. X /
  YouTube / TikTok upload bytes directly.
- **`WebSearchTool` is hosted** (runs in the Responses API). If a deployment/region lacks it, swap
  [tools/web_search.py](aismm/tools/web_search.py) for a fallback (LangChain `{"type":"web_search"}`,
  Tavily, DDG) — one file.
- **Async from sync**: orchestrator/dashboard drive async agent+platform calls via `asyncio.run`
  (see `orchestrator._run_async`, dashboard OAuth callback). Keep platform methods async.
- **Secrets**: `.env`, `tokens.key`, and `data/` are git-ignored. Never commit tokens or print
  decrypted ones. The dashboard has no auth of its own.
- **Live posting** requires real, approved developer apps (IG App Review, TikTok audit → else
  `SELF_ONLY`, YouTube quota). Default new instructions to `dry_run`.
