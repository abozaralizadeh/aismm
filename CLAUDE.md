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
pytest -q                       # 30 unit tests (no network, no creds needed)
python scripts/smoke_llm.py     # verifies Azure/APIM LLM wiring (needs LLM creds)
python scripts/smoke_sora.py    # generates one Sora clip (skips if unconfigured)
python scripts/smoke_sora.py --pool   # print the Sora resource pool, no API calls
```

There is no lint/format config; match the surrounding style (stdlib logging, `from __future__ import
annotations`, type hints, ~100-col lines).

A shared [`.vscode/`](.vscode) config ships launch profiles (run / dashboard-only / scheduler-only /
post-one / smoke tests / pytest-current-file / attach) and tasks (venv setup, `.env` bootstrap,
pytest, gunicorn). `justMyCode` is off so breakpoints hit the agent and SDK; the Flask reloader is
off so the debugger keeps its process. README's "Debugging in VS Code" has the profile table.

## Architecture & data flow

`Instruction` (dashboard-authored: brief + accounts + schedule + publish_mode) → **APScheduler**
([scheduler.py](aismm/scheduler.py)) fires → [orchestrator.py](aismm/orchestrator.py)
`run_instruction()` loops selected accounts, takes a **single-flight lock**, creates a `Run`, and
calls the agent → [agent/manager_agent.py](aismm/agent/manager_agent.py) builds an `Agent` with
per-run tools and does `Runner.run` + deterministic recovery → the agent finishes by calling the
**`publish`** tool, which **gates on `instruction.publish_mode`** in code.

**A run has two terminal tools**: `publish` OR `report_failure`
([tools/failure_tool.py](aismm/tools/failure_tool.py)). Publishing is not mandatory — the prompt
previously said "Always finish by calling publish", and a blocked agent duly generated a video and
published a caption explaining the problem. Keep all four parts intact: the prompt's "WHEN YOU CANNOT
DO THE JOB" section, the recovery nudge offering both endings, `meta_caption_reason` in publish_tool
(refuses first-person failure captions; keep it narrow — blocking real copy is worse), and the
no-terminal-call fallback that marks the run failed.

`perform_publish` validates before it calls a platform, in this order: capability (can it post this
kind?) → placement (carousel/story allowed? item cap? known placement?) → **media actually present
and on disk/blob**. That last check exists because `media_kind="image"` with no `asset_path` used to
sail through the image branch and fail inside Instagram with a generic "needs a media asset". Keep the
cheap declarative checks first — "this platform has no stories" beats "asset missing" when both are
true. Errors name the tool to call, since the agent acts on the message.

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
- **App credentials come from `platforms/apps.resolve_creds`**, not `settings` — `.env` and
  dashboard-managed `PlatformApp` rows (several per platform, secret Fernet-encrypted) coexist and
  are BOTH always offered; `.env` is the default so a pre-existing account keeps resolving to the
  credentials that created it. `ENV_APP_ID` ("env") requests `.env` explicitly, since an empty
  `app_id` can't distinguish "no preference" from "the .env one". Only the
  OAuth connect needs them; publishing uses the stored token, so `get_platform(name)` without creds
  is fine there. Add a platform → add a `setup_guides.GUIDES` entry too, or its Apps page shows a
  bare placeholder.
- **Platforms subclass `SocialPlatform`** ([platforms/base.py](aismm/platforms/base.py)): declare
  OAuth endpoints/scopes + `Capabilities` as class attrs, implement `fetch_identity` + `publish`,
  then `register(PlatformName.x, Cls)`. Generic OAuth (authorize URL / code exchange / refresh) is
  inherited; override only when a platform differs (TikTok uses `client_key`, so it overrides them).
- **Run listing is paged/filtered/sorted in the STORE** (`list_runs` + `count_runs`), never in the
  view — the run table grows without bound. Sort keys are whitelisted (a query param must not reach
  arbitrary columns), search covers caption/error/log/url plus the instruction *name*, and
  LocalStore does it in SQL while AzureStore does it in Python (Table Storage can't). Fetch one run
  with `get_run`, not by scanning a list.
- **Storage goes through the `Store` interface** ([store/base.py](aismm/store/base.py)). Two
  implementations: `LocalStore` (SQLite) and `AzureStore` (Table storage), chosen by
  `settings.use_azure_store`. Never read/write the DB directly from routes/agent code — call the
  store, and add new methods to `base.py` + **both** backends — a backend missing one only fails at
  `get_store()`, i.e. at gunicorn worker boot, as a crash loop. `tests/test_store_interface.py` and
  `scripts/preflight.py` both catch it; `setup_service.sh` runs the latter before restarting. Tokens cross this boundary in
  plaintext; the store encrypts (Fernet) internally.
- **Media goes through [assets.py](aismm/assets.py)**, never `open()`/`Path.read_bytes` on an
  `asset_path`: with Azure configured the bytes may live only in blob storage. `save_bytes` writes
  locally *and* mirrors to blob, `read_bytes` falls back to the blob, `public_url` returns the blob
  URL when available.
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
| [aismm/platforms/apps.py](aismm/platforms/apps.py) | which OAuth app credentials a connect uses (DB app → `.env` fallback) |
| [aismm/platforms/setup_guides.py](aismm/platforms/setup_guides.py) | per-platform "where to get these credentials" text shown on the Apps page |
| [aismm/orchestrator.py](aismm/orchestrator.py) | per-account run + lock + `approve_staged`/`reject_staged` |
| [aismm/store/](aismm/store/) | base + local_store (SQLite) + azure_store (Table) + blob_media (Blob) |
| [aismm/dashboard/app.py](aismm/dashboard/app.py) | Flask control center (accounts, instructions, runs, OAuth callbacks, `/assets`) |
| [aismm/dashboard/sso.py](aismm/dashboard/sso.py) | generic OIDC sign-in guard + `/login`, `/auth/callback`, `/logout` |
| [aismm/agent/memory.py](aismm/agent/memory.py) | post-run summarizer for an oversized carry-over memory |
| [aismm/schedules.py](aismm/schedules.py) | schedule text → APScheduler triggers (times, weekdays, intervals, cron) |
| [aismm/models.py](aismm/models.py) | SQLModel tables + `PublishMode`/`PlatformName`/`RunStatus`/… enums |
| [aismm/wsgi.py](aismm/wsgi.py) | gunicorn entrypoint — starts the scheduler, then exposes the dashboard as `application` |
| [setup_service.sh](setup_service.sh) | idempotent systemd install/update for a Linux server |

## Continuity (memory + note)

`InstructionState` ([models.py](aismm/models.py)) is a **side table**, not columns on `Instruction`:
`SQLModel.create_all` adds missing tables but never missing columns, so widening a table breaks
existing databases. Add future per-instruction state there, or write a migration.

It holds two independent things: `memory` (agent-written via `read_memory`/`update_memory`, carried
into the next run) and `note` (human-written in the dashboard, an override the agent must follow and
must not edit). Both are inlined into the kickoff by `build_kickoff` — a scheduled run only reliably
*continues* work when the previous position is in the first turn, not merely fetchable. After each
run `agent/memory.maybe_compact` summarizes an oversized memory; that LLM call lives in the agent
layer because **tools do deterministic work only**, and it fails safe (a failed compaction leaves
the memory untouched — losing the cursor would break continuity entirely).

## AI disclosure

[disclosure.py](aismm/disclosure.py) labels every post as AI-generated, applied in
`perform_publish` **beside the publish-mode gate** — deterministic, on every path (preview, approval
queue, live), never left to the model (the prompt tells it not to write its own). Two layers: the
caption suffix, and the platform's native flag where its API has one — TikTok `post_info.is_aigc`
(**not** `is_ai_generated`, which the API silently ignores) and YouTube
`status.containsSyntheticMedia`. Instagram and X have no per-post field, so the caption line is the
disclosure there. When trimming to a caption limit, cut the caption, never the label. Driven by EU AI
Act Art. 50 (applies 2 Aug 2026) plus each platform's own rule; `AI_DISCLOSURE_ENABLED=0` opts out.

## Gotchas

- **Dashboard mobile rules** ([static/style.css](aismm/dashboard/static/style.css)): a new `<table>`
  must be wrapped in `<div class="table-scroll">` or the whole page scrolls sideways on a phone, and
  form controls must stay **16px on touch** (`@media (pointer: coarse)`) or iOS Safari zooms on focus
  and never returns. `tests/test_responsive.py` enforces both; it also covers the viewport tag, the
  scrollable nav, and 44px tap targets.

- **Widening a table is now safe**: `LocalStore._add_missing_columns` runs `ALTER TABLE ADD COLUMN`
  for any column the models declare and the DB lacks (Azure Table is schemaless, so it needs
  nothing). Prefer a real column over a side table for new per-instruction fields; `InstructionState`
  stays a side table because memory/note are large, mutable and semantically separate.
- **One schedule → SEVERAL triggers** ([schedules.py](aismm/schedules.py)): `parse_schedule` returns a
  list and `refresh_jobs` registers one job per trigger (`instr:<id>`, `instr:<id>:1`, …). Ambiguous
  input is REFUSED, not guessed — a bare `6` could be 06:00 or every 6h. `describe()` is the
  dashboard readback; keep it defensive, cron fields can be `*/4`.
- **gpt-image-2 ≠ gpt-image-1** ([tools/image_tool.py](aismm/tools/image_tool.py)): image-2 takes
  arbitrary sizes (edges ×16, ratio ≤3:1), **rejects `input_fidelity` outright**, and has no
  transparent background; image-1 accepts only three sizes but supports both. `resolve_size` fixes a
  size instead of letting the API fail with an unexplained error.
- **AI disclosure is per instruction too** (`Instruction.disclose_ai` checkbox). The global
  `AI_DISCLOSURE_ENABLED` is the master — an instruction may opt out below it, never back on.

- **Multi-clip video** ([tools/sequence_tool.py](aismm/tools/sequence_tool.py) +
  [video.py](aismm/video.py)): Sora renders 4/8/12s, so longer videos are merged with
  imageio-ffmpeg's bundled binary. Three consistency levers, all applied together because GenBox
  applying one at a time still drifts: the `style` block is repeated in EVERY prompt, the reference
  frame is explicitly described as the previous shot's final frame, and the sequence is **pinned to
  one Sora resource** (remix is job-scoped — GenBox's per-clip failover destroyed it). `auto` falls
  back to remixing shot 1 when `input_reference` is refused (faces). Always re-encode before concat
  and add silence to mute clips, or the merged file loses audio from the first silent clip onward.
- **Sora 2** ([tools/sora_client.py](aismm/tools/sora_client.py)): job-scoped — a job id only exists
  on the resource that created it, so create/poll/download must stay on one resource. The pool
  round-robins **at the job level** (`sora_config.next_resource`); never front it with a round-robin
  gateway. `create_clip_with_failover` retries a failed clip on a *different* resource
  (`exclude_endpoints`) and returns the serving resource + job id, since only that resource can
  poll/download/remix the job — same scheme as SandBox/GenBox's `_safe_create`. Log Azure's response
  body on failure (`format_http_error`); httpx's message alone omits the reason. Sora 2 has **no
  seed**; `input_reference` rejects human faces. The Videos API is announced for shutdown ~Sep 24
  2026 — the tool is behind the registry so a successor can replace it.
- **APIM is an *Azure*-shaped client** ([llm.py](aismm/llm.py)): both providers build
  `AsyncAzureOpenAI`; for APIM the gateway route is the `azure_endpoint` (trAIde's
  `_build_openai_client`). A plain `AsyncOpenAI(base_url=…)` drops Azure's `/openai` segment and
  posts to `{apim}/responses` — which the gateway doesn't route — and adds a bogus
  `Authorization: Bearer`. `APIM_BASE_URL` therefore excludes `/openai`.
  [tests/test_llm_client.py](tests/test_llm_client.py) pins the resulting request URL.
- **Agent tracing must be pointed somewhere valid** — the SDK's exporter posts to `api.openai.com`
  with the *default client's* key, so on Azure/APIM it 401s on every run until `configure_tracing()`
  disables it. Call it from any entrypoint that can start a run (CLI `run`/`dashboard`/`scheduler`/
  `post`, [wsgi.py](aismm/wsgi.py)).
- **Tests must not read the developer's `.env`** — `tests/conftest.py` pins the env vars settings are
  built from *before* `aismm` is imported (config reads env at import time). Add a pin there when a
  new setting would change behavior under test.
- **Playwright browsing** ([tools/browse_tool.py](aismm/tools/browse_tool.py)): one Chromium per run,
  cached on `state["_browser"]` and closed by `manager_agent` in a `finally` — AIBlog's lesson is
  that a browser finalized later by GC raises "Event loop is closed". **Wait properly or you scrape
  the loading skeleton**: `domcontentloaded` alone returns "Generating…" and zero images on
  JS-rendered pages, so we also wait for `networkidle`, force `loading="lazy"` images eager, scroll,
  and accept a `wait_for` selector. Images are returned as `{url, alt, width, height, caption}` with
  `url` preferring `data-full`/`data-src`/`srcset` over the `src` thumbnail — an agent working
  through numbered panels needs the alt text and the surrounding dialogue, not a bare URL.
  **`save_media` sniffs bytes, never trusts `Content-Type`** (`sniff_media`): blobs uploaded without
  a content type serve `application/octet-stream`, which made real PNGs unpostable. Magic numbers →
  Pillow → content-type → URL extension, but a declared non-media type (text/html, pdf) still wins
  over the extension so a 404 page at `.jpg` isn't saved as a broken image. The browser *binary* is a
  separate install (`playwright install chromium`), per-user; `setup_service.sh` runs `install-deps`
  as root but the download as the service user, or the service can't find it. The agent picks the
  URL, so `is_public_url` refuses private/loopback/link-local addresses (cloud instance metadata).
- **Azure store = ONE table, PartitionKey per entity type** ([store/azure_store.py](aismm/store/azure_store.py)),
  the SandBox layout (`GenBox`/`ComicBook` azurestorage.py). Table Storage can't sort or paginate
  server-side (sort in Python), rejects `None`/dict/list values (`_upsert` filters + ISO-formats
  datetimes), and caps a property at 64 KB (`MAX_PROPERTY_CHARS` fails loudly first). Locks are
  `create_entity` + `ResourceExistsError` + TTL reclaim — `GenBox._try_acquire_lock`. RowKey forbids
  `/ \ # ?`, so lock keys are sanitized. Env vars accept SandBox's lowercase `connection_string`.
- **Logging must be configured or nothing below WARNING is emitted** — Python's root logger defaults
  to WARNING, so `logger.info(...)` is discarded unless `logging_setup.configure_logging()` ran.
  Call it FIRST in any new entrypoint (CLI commands, [wsgi.py](aismm/wsgi.py)); `LOG_LEVEL` picks the
  level and third-party loggers are pinned quieter unless it's DEBUG.
- **JPEGs are written BASELINE, not progressive** ([media.py](aismm/media.py)) — Meta's pipeline is
  unreliable with progressive JPEGs and only reports it as container status ERROR (2207076). Ratio
  padding also targets ~1% inside the platform's bounds; landing exactly on 0.8 is a coin flip.
- **Images are normalized before publishing** ([media.py](aismm/media.py), called from
  `perform_publish` so preview/approval/live share one converted file). Platform limits live on
  `Capabilities` (`image_formats`, `max_image_bytes`, `min/max_image_ratio`, `max_image_width`);
  only Instagram declares them today — **JPEG only**, 8MB, 4:5–1.91:1, ≤1440px — and anything else
  comes back as a misleading "Media download has failed". Flatten alpha before JPEG (Pillow raises
  otherwise), and **pad, never crop**, to fix a ratio. Conversion is best-effort: on failure pass
  the original through so the platform's error surfaces. Video isn't re-encoded (no ffmpeg dep).
- **Instagram placements** ([platforms/instagram.py](aismm/platforms/instagram.py)): carousel =
  child containers with `is_carousel_item=true` and NO caption, then a `CAROUSEL` parent carrying the
  caption + `children=<ids>`; a video child is `media_type=VIDEO` (**not** `REELS`, which is
  standalone); a story is `media_type=STORIES` and takes no caption. `publish` validates placements
  against `Capabilities` (`supports_carousel`/`supports_stories`/`max_carousel_items`) before calling
  the platform.
- **Instagram engagement tools** ([tools/instagram_tools.py](aismm/tools/instagram_tools.py)) return
  `None` from their factories unless the run targets an Instagram account, so other platforms aren't
  handed them. Reply/moderate act on the live account IMMEDIATELY — they are not behind
  `publish_mode`, which gates posts. Insight metric names churn (v21 dropped `impressions`,
  `profile_views`, non-Reels `video_views`), so `DEFAULT_*_METRICS` stays minimal and overridable.
  Comments/insights need the `instagram_manage_comments` / `instagram_manage_insights` scopes — an
  account connected before those were added must be reconnected.
- **Instagram needs a PUBLIC media URL** — it fetches media, no binary upload. Assets are served at
  `DASHBOARD_BASE_URL<REVERSE_PROXY_PREFIX>/assets/<file>`; the IG integration raises if that
  resolves to localhost. X / YouTube / TikTok upload bytes directly.
- **Graph calls send the token as `Authorization: Bearer`, never in the query string** — httpx puts
  the URL in its exception message, so a token in `params` lands in the service log. Always surface
  Graph's JSON error body (`_graph_error`): a bare `400` hides whether it's a format, permission,
  readiness (code 9007 → wait + retry, handled) or rate-limit problem.
- **Tracing needs `langsmith` installed** — it's a hard dep in requirements.txt (SandBox does the
  same). `set_trace_processors([OpenAIAgentsTracingProcessor()])` only; never add `wrap_openai` or
  `@traceable` on the same calls (duplicates traces, flattens the span tree).
- **`WebSearchTool` is hosted** (runs in the Responses API). If a deployment/region lacks it, swap
  [tools/web_search.py](aismm/tools/web_search.py) for a fallback (LangChain `{"type":"web_search"}`,
  Tavily, DDG) — one file.
- **Async from sync**: orchestrator/dashboard drive async agent+platform calls via `asyncio.run`
  (see `orchestrator._run_async`, dashboard OAuth callback). Keep platform methods async.
- **Secrets**: `.env`, `tokens.key`, and `data/` are git-ignored. Never commit tokens or print
  decrypted ones.
- **Dashboard SSO** ([dashboard/sso.py](aismm/dashboard/sso.py)): generic OIDC (Google/Entra/Okta —
  endpoints come from the issuer's discovery doc), enabled as soon as `AUTH_OIDC_*` is set. A
  `before_request` guard blocks every endpoint except `sso.PUBLIC_ENDPOINTS`. **Keep `asset` in that
  set** — Instagram fetches media server-side with no cookie, so guarding `/assets` breaks
  publishing. Authentication alone grants nothing: `AuthSettings.allows()` fails **closed** when the
  allowlist is empty. ID token signatures are intentionally unverified (back-channel TLS fetch, OIDC
  Core §3.1.3.7) — iss/aud/exp/nonce are checked; don't move ID tokens to the front channel without
  adding JWKS verification.
- **One worker only** when serving `aismm.wsgi:application`: the dashboard re-syncs APScheduler jobs
  *in-process* (`scheduler.refresh_jobs`), so extra workers each get their own scheduler that the
  dashboard never talks to. `setup_service.sh` pins `--workers 1 --threads N` for this reason. Set
  `AISMM_ENABLE_SCHEDULER=0` (read in [config.py](aismm/config.py), honored by
  [wsgi.py](aismm/wsgi.py)) to serve the dashboard alone; `aismm run`/`scheduler` ignore the flag.
- **Live posting** requires real, approved developer apps (IG App Review, TikTok audit → else
  `SELF_ONLY`, YouTube quota). Default new instructions to `dry_run`.
