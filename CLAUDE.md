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
python -m aismm.cli reconcile [--apply]   # fix runs marked failed whose post is actually live

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
  the tool docstring is what the model sees. The ONE exception is `describe_image`
  ([tools/vision_tool.py](aismm/tools/vision_tool.py)), which is inherently a model call: it keeps
  the rule as far as it can by doing the deterministic half itself (resolve target, fetch, sniff,
  reject video, downscale) and delegating only the looking to
  [agent/vision.py](aismm/agent/vision.py) — same layering as `agent/memory.py`'s compaction. The
  describer is a **separate** small agent: it must not inherit the manager's instructions, memory or
  tools, and must not be able to publish.
- **App credentials come from `platforms/apps.resolve_creds`**, not `settings` — `.env` and
  dashboard-managed `PlatformApp` rows (several per platform, secret Fernet-encrypted) coexist and
  are BOTH always offered; `.env` is the default so a pre-existing account keeps resolving to the
  credentials that created it. `ENV_APP_ID` ("env") requests `.env` explicitly, since an empty
  `app_id` can't distinguish "no preference" from "the .env one". Only the
  OAuth connect needs them; publishing uses the stored token, so `get_platform(name)` without creds
  is fine there. Add a platform → add a `setup_guides.GUIDES` entry too, or its Apps page shows a
  bare placeholder.
- **`publish()` MUST take the full keyword set** — `perform_publish` always passes `asset_paths`
  and `placement`. Python doesn't check override signatures, so Twitter/YouTube/TikTok kept the old
  narrower signature after Instagram grew carousels, and the first ever X publish died with
  `got an unexpected keyword argument 'asset_paths'` — *after* a full run of browsing and image
  generation. `tests/test_store_interface.py` and `scripts/preflight.py` now both bind the real call
  against every registered platform, so adding a platform (or widening the base signature) fails
  before deploy rather than on someone's first post.
- **X auto-threads past 280** (`twitter.split_thread`, `Capabilities.supports_threads` /
  `max_thread_posts`). `caption_limit` bounds ONE post, so `perform_publish` gives a threading
  platform `caption_limit * max_thread_posts` as the disclosure budget — trimming to 280 there would
  cut the caption before X could split it. Media goes on the FIRST post only (X repeats it otherwise)
  and the **AI label is pinned to the first post** via `pin_suffix`: appended at the end it would
  land on `n/n`, unseen by anyone who only meets `1/n` in a timeline, which is the "first exposure"
  the label is for. A failure mid-thread reports what already went out instead of losing those ids.
- **Platform-specific tool modules mirror each other** (`instagram_tools.py`, `twitter_tools.py`):
  every factory returns `None` unless the run targets that platform, so an IG run isn't handed the
  six X tools. Write tools (reply, delete) act immediately and are deliberately NOT behind
  `publish_mode` — that gate is about posts. **X is pay-per-use with no free tier** (since Feb 2026):
  an account out of credits gets 402 on *everything*, posting included, and httpx's bare "Client
  error '402 Payment Required'" told the agent nothing. `Twitter._api_error` explains 402 (billing,
  buy credits at console.x.com), 401/403 (token) and 429 (rate limit) distinctly, and EVERY X call
  path routes through it — `_upload_media` and `fetch_identity` used `raise_for_status()` and so
  leaked the raw message. **X 5xx is X, not you** — 503 on `/2/media/upload/initialize` or `POST /2/tweets` is a
  frequent, documented X-side outage that has run for hours on ONE endpoint while the others
  worked. `_api_error` says so explicitly and points at republish, because the reflex (regenerate
  the media, rewrite the caption, reconnect the account) costs money and fixes nothing.
  `scripts/diagnose_x.py` probes read + media-initialize per account **without posting** to say
  which endpoint is affected. Media *initialize* is retried (502/503/504, 3 attempts) because it
  cannot have created anything yet; FINALIZE and `POST /2/tweets` are deliberately NOT retried —
  X may have accepted them even when the response is lost.
- **Report `errors[]` as well as `detail`** — on a 400 the top-level detail
  is the generic "One or more parameters to your request was invalid" while `errors[].message` names
  the actual parameter.
- **Several communities ROTATE, they do not fan out** (`twitter.next_community` +
  `after_publish`). One post per run, to the next id in `meta["community_ids"]`; the cursor advances
  only once the post is LIVE, so a failed attempt doesn't skip a community for a whole cycle.
  Fan-out — the same content to every community at once — is several near-identical posts from one
  account within seconds, which is what X's duplicate-content rule describes; it would also multiply
  the cost of a pay-per-use API and force the publish ledger to be keyed per community, weakening the
  guard that stops accidental repeats. A scheduler covers every community anyway, with different
  content each run. The whole of a THREAD goes to one community, chosen once. `after_publish` is a
  `SocialPlatform` hook (default no-op) rather than another platform branch in `perform_publish`, and
  its failure is logged, never fatal — the post already went out.
- **The destination is per INSTRUCTION, with the account as the default**
  (`Instruction.twitter_community_id` / `twitter_share_with_followers`, read by `next_community`
  and `shares_with_followers`). One account commonly runs a niche-community instruction and a
  timeline instruction; an account-wide setting cannot express that. `""` inherits the account's
  rotation, `HOME_TIMELINE` (`"none"`) forces the timeline, anything else is one id — and a pinned
  instruction **must not advance the rotation cursor** in `after_publish` (it never used the
  rotation; advancing would walk the cursor past the communities the rotating instructions feed),
  which is why `after_publish` grew an `instruction` argument. `twitter_share_with_followers` is a
  tri-state STRING (`""`/`"yes"`/`"no"`) because Azure Table storage rejects `None`. A saved pick is
  validated against the ids the workspace's accounts actually have, so a removed community falls
  back to inheriting rather than posting somewhere the operator can no longer see.
- **Communities are chosen by NAME, never by id.** A 19-digit number is not something anyone
  recognises. `GET /2/communities/:id` and `GET /2/users/:id/communities` both return names, so
  `resolve_community_names` tries the listing first (one call for all of them) and falls back to
  per-id lookups, caching the result in `account.meta["community_names"]` at save time — X is
  pay-per-use, so this is not done per render. Both endpoints can fail (app tier, a community the
  account has not joined), so the operator can label an id by hand: `parse_community_entries`
  accepts `ID = Name`, **one entry per line**, because a name may contain commas and commas
  separate bare ids. A typed label WINS over the API's — it is deliberate. An id with no name
  displays as the id, never as blank.
- **A community post is invisible to your followers unless you say otherwise** — `POST /2/tweets`
  takes `community_id` AND `share_with_followers` (boolean, default false), which is the switch X's
  own composer shows beside the community picker. Both live in `account.meta` and are set per
  connection on the Accounts page. `share_with_followers` is sent ONLY alongside a `community_id`
  (it means nothing on a normal post) and is cleared when the community is cleared, so it cannot
  silently apply to a community set later. It goes on EVERY post of a thread: the later posts belong
  to the same community and the same audience.
- **X media upload uses the SUB-PATH endpoints, not `command=`**
  ([platforms/twitter.py](aismm/platforms/twitter.py) `_upload_media`): `POST
  /2/media/upload/initialize` (**JSON**, `total_bytes` a real integer), `/{id}/append` (multipart,
  `segment_index`), `/{id}/finalize`. X still documents the legacy `command=INIT|APPEND|FINALIZE`
  form-parameter shape on `POST /2/media/upload`, but it 400s — that is the migration-era shell.
  Only the STATUS poll still goes through `GET /2/media/upload?command=STATUS`. `media_type` is
  **sniffed from the bytes** (`_media_type`), since `initialize` validates it and a PNG announced as
  `image/jpeg` is a 400.
- **Access tokens EXPIRE, and `tokens.valid_access_token` is the only supported way to get one**
  ([tokens.py](aismm/tokens.py)). X dies in ~2h, YouTube in 1h; Instagram's 60-day page tokens hid
  this until X was connected, and an account that published fine yesterday answered 401 on
  *everything* the next morning. The refresh token was captured at connect and stored from the first
  version — nothing ever spent it, because every call site read
  `access_token, _refresh = store.get_tokens(...)` and threw the second half away. Never write that
  line again. Refreshing is **best effort**: on failure the stored token is returned so the
  platform's own 401 surfaces (a crash here would be worse), and no `expires_at` means "unknown",
  which is left alone rather than guessed. Refreshes are **locked per account and re-checked under
  the lock** — X rotates the refresh token on use, so a second refresh from a stale in-memory
  `Account` spends the replacement and kills the grant; the lock alone doesn't stop that, the
  re-read does. A platform returning no new refresh token keeps the old one (Google omits it).
- **Platforms subclass `SocialPlatform`** ([platforms/base.py](aismm/platforms/base.py)): declare
  OAuth endpoints/scopes + `Capabilities` as class attrs, implement `fetch_identity` + `publish`,
  then `register(PlatformName.x, Cls)`. Generic OAuth (authorize URL / code exchange / refresh) is
  inherited; override only when a platform differs (TikTok uses `client_key`, so it overrides them).
- **Run listing is paged/filtered/sorted in the STORE** (`list_runs` + `count_runs`), never in the
  view — the run table grows without bound. Sort keys are whitelisted (a query param must not reach
  arbitrary columns), search covers caption/error/log/url plus the instruction *name*, and
  LocalStore does it in SQL while AzureStore does it in Python (Table Storage can't). Fetch one run
  with `get_run`, not by scanning a list.
- **Workspaces scope accounts, instructions, runs and staged posts** ([workspaces.py](aismm/workspaces.py)).
  A workspace is a SILO, not just an instruction folder — scoping instructions alone would still let
  every member publish to every connected account, so a private workspace would not be private.
  There is **one kind**: private to its creator until they add a member, shareable in every case. A
  separate "personal" kind that could also be shared was a distinction without a difference.
  **A first sign-in gets you your OWN workspace and nothing else** — a new colleague must not land in
  someone else's live accounts — and `landing()` prefers a workspace you `created_by` over one you
  were invited to. Identity is the SSO email (lowercased); there is no user table. **With SSO off the
  dashboard is already unauthenticated**, so one implicit local operator (`LOCAL_USER`) owns
  everything rather than a fictional user guarding nothing — the `unauthenticated=True` branch is
  not a bug. Rows predating workspaces carry `workspace_id=""` and exactly ONE workspace
  (`claims_unassigned`) owns them **at read time**: `scope_for()` returns `[id, ""]` for it, and
  every read about a workspace must go through `scope_for`/`content_counts` — listing through the
  scope while counting through the bare id is what showed a full page of instructions above "0
  instruction(s)". Read-time claiming beats a boot-time rewrite because a migration that is skipped,
  interrupted, or run against an unbounded runs table cannot lose anything this way (`adopt_orphans`
  remains an opt-in tidy-up via `cli workspaces --adopt`). The first operator to sign in **claims**
  that workspace and it is RENAMED to theirs (`_claim`): an earlier build left them a second, empty
  workspace to land in while their real instructions sat elsewhere, which reads as data loss. `_claim`
  also repairs the ownerless workspace that build created — with no owner, its membership could never
  be changed by anyone; `cli workspaces --owner EMAIL [--workspace ID_OR_NAME]` is the manual route
  (`workspaces.make_owner`, promote-only, never demotes an existing owner). In the dashboard every scoped read goes through `_workspace_id()` and every
  by-id fetch through `_owned()` (404, not 403 — whether an id exists elsewhere is not the caller's
  business); a route that forgets either one leaks another workspace's data. `_new_workspace_id()` is
  the single-id variant for rows being CREATED. Platform *apps* are deliberately NOT scoped: they are
  deployment infrastructure, and the sensitive half (the token) lives on the scoped `Account`.
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
  URL when available. **With blob configured the local folder is a CACHE, not the archive** —
  media is tens of MB per clip, and a full disk stops everything (the next run can't write media,
  gunicorn can't log, SQLite can't commit). `assets.prune_local` + `orchestrator.prune_asset_cache`
  run daily (`scheduler._schedule_housekeeping`, cron `30 4 * * *`, plus a boot sweep) and drop local
  files older than `ASSET_RETENTION_DAYS` (14). The safety rule is absolute: **a file is deleted only
  after `blob_media.exists()` confirms the blob copy** — no blob configured is a NO-OP, an
  unreachable blob KEEPS the file. The media of the last 200 runs is spared regardless of age, and
  `/assets/<file>` falls back to streaming from blob (preserving `?download=1`) so a pruned file
  still previews and still republishes. **Every `<img>`/`<video>` in the dashboard goes through the
  `media_url()` template global**, which returns the BLOB url so the browser fetches media from
  storage and the VM is not in the path — a `url_for('asset', …)` in a new template quietly routes it
  back through the VM, and a test asserts no template does. It falls back to `/assets` when the
  container is not anonymously readable (`blob_media.public_read()`; `None` means *could not ask* and
  is treated as "don't risk it" — our own route always works, so an unknown is never worth a broken
  preview) and **always** for `download=True`: a blob URL cannot set `Content-Disposition:
  attachment` without a SAS token, and that header is the only way to save a video out of iOS Safari. `ASSET_RETENTION_DAYS=0` turns the prune OFF rather than
  meaning "delete everything" — zeroing a retention setting is switching it off; `cli assets
  --older-than 0 --apply` is the explicit way to clear the lot. Housekeeping must never stop the
  scheduler booting: both the job registration and the sweep swallow and log their failures.
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
| [aismm/workspaces.py](aismm/workspaces.py) | workspace membership, roles, and the read-time migration |
| [aismm/agent/memory.py](aismm/agent/memory.py) | post-run summarizer for an oversized carry-over memory |
| [aismm/agent/vision.py](aismm/agent/vision.py) | small vision agent behind the `describe_image` tool |
| [aismm/schedules.py](aismm/schedules.py) | schedule text → APScheduler triggers (times, weekdays, intervals, cron) |
| [aismm/models.py](aismm/models.py) | SQLModel tables + `PublishMode`/`PlatformName`/`RunStatus`/… enums |
| [aismm/wsgi.py](aismm/wsgi.py) | gunicorn entrypoint — starts the scheduler, then exposes the dashboard as `application` |
| [setup_service.sh](setup_service.sh) | idempotent systemd install/update for a Linux server |

## Attachments and the stored prompt

`Run.prompt` holds the kickoff each run actually received. Debugging a failure means reading what the
agent was told, not what the instruction says now — the run detail page shows it in a `<details>`
block alongside the *current* brief/memory/note for comparison.

`InstructionFile` ([models.py](aismm/models.py)) is a file attached to an instruction, with an
`AttachmentPurpose`: **context** (text extracted ONCE at upload by
[attachments.py](aismm/attachments.py) and stored on the row — never re-parsed per run; an excerpt
goes in the kickoff, `read_attachment` serves the rest) or **reference** (an image whose `asset_path`
the generators receive). Extraction is best-effort: a scan with no text layer still uploads and says
why it is empty. Office formats are deliberately unsupported — "export to PDF" covers it without
another dependency.

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
queue, live), never left to the model.

**All four platforms expose a NATIVE flag in their publishing API, and that is the real label** —
the same switch their apps show a human ("Add AI Label" in Instagram's composer, "Made with AI"
under X's content disclosures): Instagram `is_ai_generated` (on the container; on a carousel it goes
on the PARENT only, since the children are not posts), X `made_with_ai` (on EVERY post of a thread —
each stands alone in a timeline), TikTok `post_info.is_aigc` (**not** `is_ai_generated`, which the
API silently ignores), YouTube `status.containsSyntheticMedia`. An earlier version of this file
asserted Instagram and X had no such field, so those two were labelled with a caption sentence
instead — prose where the platform offered a rendered badge. Check the API before concluding a
platform has no flag.

**The caption suffix is now opt-in** (`AI_DISCLOSURE_CAPTION=1`, default off): with a native label
everywhere it is redundant, and a platform-rendered badge cannot be mistaken for the author's own
words. When on, trimming to a caption limit cuts the caption, never the label. Driven by EU AI Act
Art. 50 (applies 2 Aug 2026) plus each platform's own rule; `AI_DISCLOSURE_ENABLED=0` opts out of
both layers.

## Engagement runs (responding to comments)

An instruction has a **`task_type`** (`Instruction.task_type`, `InstructionTask.publish` |
`engage` | `auto`). A `publish` run researches and posts one thing; an **engage** run reads new
comments/mentions on the account and replies in its voice, on the same cron schedule. The two share
the whole pipeline (scheduler → orchestrator → `run_for_account`) and diverge only where it matters:
`manager_agent` picks `ENGAGEMENT_INSTRUCTIONS` + `build_engagement_kickoff`, and `registry` swaps
the terminal tool set — `ALWAYS_ON_ENGAGE` = (`finish_engagement`, `report_failure`) vs
`ALWAYS_ON_PUBLISH` = (`publish`, `report_failure`). The sets are **disjoint and picked by
`task_type`** (`always_on_for`): the wrong terminal is worse than none — an engage run offered
`publish` would post a thing it was never asked to — so `build_tools` withholds the other task's
terminal even if the picker names it.

**`auto` lets the AGENT decide, per run, whether to publish or engage.** `manager_agent` picks
`AUTO_INSTRUCTIONS` (a decision-router prompt) + `build_auto_kickoff`, and the terminal set is
`ALWAYS_ON_AUTO` = (`publish`, `finish_engagement`, `report_failure`) — `ALWAYS_ON` now aliases
this. Auto is the ONE case that keeps BOTH task terminals: it deliberately relaxes the
disjoint-terminal rule because the operator asked the agent to choose, so `build_tools` does not
withhold either ending for an auto run. The recovery nudge and the `no_terminal_call` fallback in
`run_for_account` branch three ways (`auto` / `engage` / else), each naming that run's valid
endings. The prompt still requires the agent to do exactly ONE job and finish with the single
terminal that matches it.

**Replies obey the publish-mode gate, exactly like posts.** [engagement.py](aismm/engagement.py)
`perform_reply` is the mirror of `perform_publish`: `dry_run` → `StagedPost(action_type="reply",
preview)`, `approval` → `StagedPost(pending_approval)` (dashboard Approve → `orchestrator.
approve_staged` branches on `action_type=="reply"` and sends via `reply_to_target`), `live` → send
now. ONE staged queue serves both — `StagedPost` grew `action_type`/`target_type`/`target_id`/
`target_excerpt` and the reply text reuses `caption`. Earlier the reply tools posted immediately;
they are now gated so an autonomous reply bot can be supervised. The write tools that ACT (moderate,
delete) stay un-gated — that gate is about outbound content.

**A cron engage run re-reads the same thread every fire, so two guards run before anything is staged
or sent** ([engagement_ledger.py](aismm/engagement_ledger.py), same `account.meta` fingerprint
pattern as [publish_ledger.py](aismm/publish_ledger.py) — no schema change, both backends,
`MAX_ENTRIES`-bounded): (1) a target already REPLIED to (`engagement_ledger.answered`, keyed on
`{target_type}:{target_id}` — the upstream item, never the reply text the agent rewrites each run);
(2) a still-OPEN staged reply for the same target (`Store.open_staged_reply_keys`, SQL on LocalStore
+ scan fallback in `base.py`), so the queue doesn't fill with duplicates of one unanswered comment.
A *rejected* reply is neither, so it can be reconsidered next run. The read tools annotate each item
`already_answered` from the ledger so the agent skips it up front. `finish_engagement` is the
non-failure ending: it reads the per-run tally on `state["engagement"]` (kept in code by
`perform_reply`, not reported by the model) and sets the run status published/staged/skipped.

**`outreach` is the OTHER-directed engagement task: find strangers' posts and engage them, to grow
reach.** engage answers people on your OWN posts; outreach goes looking. It reuses the whole
engagement pipeline unchanged — same terminal set (`always_on_for("outreach")` returns
`ALWAYS_ON_ENGAGE`, so an outreach run cannot `publish` either), same `perform_reply` gate, same
`engagement_ledger` (keyed `tweet:` on X, `submission:` on Reddit, so the search read's
`already_answered` flag and the reply's recorded fingerprint line up). `manager_agent` picks
`OUTREACH_INSTRUCTIONS` + `build_outreach_kickoff`; the recovery nudge and `no_terminal_call`
fallback treat outreach exactly like engage. It gets NO `performance` block (it posts nothing to
have metrics on). What the operator types goes in `Instruction.engagement_targets` (a free-text
column, `parse_targets` → typed buckets in [targets.py](aismm/targets.py): keywords, `#hashtags`,
`r/subreddits`, `@accounts`); the kickoff inlines them, or tells the agent to INFER a few from the
brief when empty.

**Outreach is X + Reddit ONLY, because those are the only two with a genuine third-party
content-search API** — the "best way per platform" is *not* to fake it on the others.
`Capabilities.supports_search` + `SocialPlatform.search_content(access_token, account, *, query,
limit, subreddit)` (base raises; `subreddit` is Reddit-only but stays in the shared signature per the
`publish` full-keyword-set lesson). Only X (`GET /2/tweets/search/recent`, narrowed to
`-is:retweet -is:reply -from:{me}` and excluding own posts by `author_id`) and Reddit
(`/r/{sub}/search` · `/r/{sub}/new` · site-wide `/search`, dropping NSFW and own posts) declare it.
Instagram hashtag search is deprecated/gated, YouTube search costs 100 quota units/call and is
spam-filtered, TikTok has no such API — all three declare `supports_search=False`, documented in the
base.py comment. `search_content` has the same drift guard as publish/reply/like/metrics
(`scripts/preflight.py` + `tests/test_store_interface.py` bind it against every `supports_search`
platform). The search tools (`x_search_posts`, `reddit_search_posts`) fall back to the instruction's
targets when called with no query, annotate each hit `already_answered`, and gate on platform like
every other per-platform tool; `reddit_reply` routes through `perform_reply`. Reddit needs NO
reconnect — its existing `read`/`submit` scopes already cover search + comment. Azure's
`_instruction_to_entity`/`_from_entity` whitelist must carry `engagement_targets` (it is an explicit
whitelist, not a dump — a new Instruction column silently vanishes on Azure otherwise).

**`supports_comments` gates a platform in or out.** Instagram/X/YouTube declare it; **TikTok does
NOT** — its comment API is audit-gated for third-party apps, so `tiktok_tools` factories return
`None` and log once rather than pretending. Adding a comment-capable platform means implementing
`reply_to_target(access_token, account, *, target_type, target_id, text, reply_to="")` (base
raises) — and `scripts/preflight.py` + `tests/test_store_interface.py` bind that call against every
`supports_comments` platform, the same drift guard as `publish`. X has no "comments" endpoint, so
`twitter.list_replies` searches recent replies under the account's own posts by `conversation_id`.
**`list_replies` MUST exclude the account's OWN replies by numeric `author_id`, not just the
`-from:{handle}` search string.** A reply the account already made is a reply in the same
conversation, so it comes straight back on the next fire; the `-from:` clause is the only thing
keeping it out, and an empty/renamed handle (or a case the operator doesn't match) lets it through —
the agent then answers its OWN reply, a fresh id the ledger has never seen, posting a second reply
under a comment it already handled (observed live on X, and X-specific: Instagram keeps replies
nested under the top-level comment, so `list_comments` never surfaces them as new items). Excluding
on `author_id == account.external_id` (from the `expansions=author_id` the search already requests)
can't slip that way; the same pass de-duplicates by tweet id. The ledger prevents a repeat of the
*same* id — the author-id filter is what stops a *new* self-reply id from ever being offered.
**DMs are the ENGAGE task, not a new task type** — answering incoming messages on your own account
is engagement, so the DM read/reply tools ride the same engage/auto tool sets and the same
`perform_reply` gate as comments (`dry_run` previews, `approval` queues, `live` sends). `supports_dms`
gates a platform in: **X, Instagram and Reddit declare it; YouTube and TikTok have no DM API** and
inherit the refusing base `list_dms`/`reply_to_target`. `scripts/preflight.py` +
`tests/test_store_interface.py` bind `list_dms` against every `supports_dms` platform (the DM *reply*
rides `reply_to_target`, already covered because every DM platform is comment-capable). No
AI-disclosure suffix on a reply or a DM (it is conversational, not a labelled post).

**Instagram MESSAGING hangs off the PAGE, not the IG user id** — this is why engage runs never
saw a single Instagram DM. Publishing is addressed to the IG user (`/{ig-user-id}/media`), but with
Instagram-via-Facebook-Login the messaging endpoints are `GET /{page-id}/conversations?platform=
instagram` and `POST /{page-id}/messages` (Meta's own Instagram Messaging guide). Asking the IG user
id for `/conversations` errors. `Instagram._messaging_target` is the single place that decides:
`meta["page_id"]` (recorded at connect by BOTH `fetch_identity` and `fetch_identities`), falling
back to **`me`** for accounts connected before it was stored — the account token IS the page token,
so `me` is the Page, and no reconnect is forced. `instagram_manage_messages` stays in
`OPTIONAL_SCOPES`. **Do NOT add `pages_manage_metadata` to the default scopes** — Meta's older
Messenger guide lists it for Instagram messaging (mostly for webhook subscription, which this app
does not use: it polls `/conversations`), but it is not offered on every app's Permissions and
Features page, and putting it in `DEFAULT_SCOPES` broke a working login outright with
`Invalid Scopes: pages_manage_metadata` — the exact hazard this file already warns about, repeated.
It lives in `EXTRA_SCOPES`: documented, never requested, opt in via `INSTAGRAM_SCOPES` if your app
has it. `list_dms` raises Graph's own error plus what to check, so whether a scope is really the
blocker is answered by Meta rather than guessed.

**A run must never report finding nothing when it had no tool to look with.** An engage run
summarised "scanned ... and inbound DMs; no new comments or DMs needed replies" on an account with
unanswered DMs: `Instruction.tools_json` narrows what `build_tools` offers, and a list ticked before
the DM tools existed never receives them — so the run truthfully found nothing, having never looked,
in words that read as "your inbox is empty". `manager_agent._engagement_gaps` compares the
platform's `Capabilities` against the tool names actually built and passes them to the kickoff as
`unavailable`; `prompts._unavailable_block` tells the agent it cannot see them, must not claim it
checked, and must SAY SO in the summary. It is logged as a warning, and
`dashboard._engagement_tool_gaps` puts the same warning on the instruction's edit page, naming the
tool to tick. The engage/auto kickoffs now say "AND its inbound DMs … every read tool you have" —
the old "(and DMs, if a DM tool is available)" invited skipping them.

**A run cannot END claiming it checked an inbox it never opened** (`engagement.note_read` →
`engagement_finish.unread_inboxes`). With the DM tools present and no error in the log, an engage
run still reported "Read comments across 12 recent posts/reels, all recent mentions, and inbound
DMs; no comments or DMs needed replies" — the model simply never called `instagram_dms`, and that
sentence is prose it wrote about itself. So the DM read tools record the call in CODE and
`finish_engagement` compares the two, sending the agent back to look. Same reasoning as the publish
ledger and the AI disclosure: a guarantee that must hold on every path cannot live in model-written
prose. It is **bounded** (`_MAX_NUDGES`) — a model that will not look must still be able to end, or
it burns the whole run on this exchange and leaves no record; after the nudges the run finishes and
the summary says "NOT CHECKED this run: …". `scripts/diagnose_instagram.py` answers the same
question outside a run: it prints the messaging node, the scopes, and Graph's own error.

**"0 replied, 0 staged, 0 skipped" must not be able to mean two different things.** A promotional
DM arrived, the agent classified it as spam and skipped it — which the ENGAGE prompt explicitly
tells it to do — and the run reported the same counts an empty inbox produces, so the operator had
no way to tell "nothing was waiting" from "I chose to answer nothing". The read tools now record
how many items were *unanswered and answerable* (`engagement.note_read(..., unanswered=N)` →
`state["engagement_seen"]`), and `finish_engagement` appends a NOTE when that is non-zero and
nothing was replied or staged. Counted in code for the same reason as the read guard above: the
summary is model-written prose. The prompt also requires naming what was skipped and why, and says
a cold sales pitch counts as spam **unless the brief or note says to answer everything** — that is
the operator's call, not the model's.

**`list_dms`'s `limit` counts CONVERSATIONS, and every one contributes its latest inbound
message.** The first version applied it three ways at once — one page of conversations, N messages
each, then the newest N messages overall — so a quiet old thread holding one unanswered question
lost to three chatty recent ones ("it gets the new messages but can't see the old ones"). The unit
someone is waiting on is a THREAD, so each is represented before any thread gets a second message,
and `/conversations` is PAGED like `list_media`. Three limits are Instagram's, not ours, and are
documented at the read so nobody hunts them again: a **Requests**-folder thread inactive for 30+
days is not accessible to the API at all (and a DM from a non-follower STARTS in Requests until it
is accepted in the app), only the **20 most recent** messages of a conversation carry detail, and
folder information is not exposed. Each item carries `age_hours`/`can_reply`: Instagram refuses an
automated reply more than **24 hours** after the person's last message. **Never send
`HUMAN_AGENT`** — it extends the window to 7 days but Meta requires a real person to apply it and
names loss of API access as the consequence of using it for automation.

**The COST of a `/conversations` call is `limit` × `messages.limit`, and Graph refuses an
expensive query rather than trimming it.** 50 × 20 = 1000 message objects answered `500 Please
reduce the amount of data you're asking for [code=1]` on the one account that actually had DMs,
while the quiet account it was tuned on worked — so the first page is now `DM_CONVERSATION_PAGE`
(15), and `_read_conversations` HALVES both the page and the nested message limit and retries **the
same cursor** on `TooMuchData`, down to a floor. Only that error is retried: a permission or token
failure fails identically at any size, and `code=1` is Graph's catch-all "API Unknown", so the
MESSAGE is what identifies it — matching on the code alone would swallow real errors.

**Subcode 2534041 is a switch in the Instagram APP, not a permission** — "The account owner has
disabled access to instagram direct messages". No scope, token or reconnect fixes it; the owner has
to turn *Settings → Messages and story replies → Connected tools → Allow access to messages* back
on. `list_dms` swaps its whole remediation paragraph for that one, because the standard "reconnect
and check your scopes" advice sends the operator the wrong way for hours.

**`list_dms` RAISES; it must never swallow a failure into `[]`.** That swallow is what hid the bug
above for weeks: an account that *could not read* DMs looked identical to an account with none. All
three platforms' tool wrappers already turn an exception into `{"error": …, "message": …}` the agent
can act on, so the platform-level `except: return []` only destroyed information. Same reasoning as
`_confirm_duplicate`'s three-state return — "no" and "cannot tell" are different answers.

**A DM carries TWO ids, and confusing them double-answers or misdelivers.** `target_id` is the
inbound MESSAGE id — the ledger dedupe key (reply once per message, keyed on `dm:<id>`) and the
open-staged guard key. The SEND destination is separate and travels as the new optional `reply_to`
on `reply_to_target`/`perform_reply`, persisted on `StagedPost.target_conversation` so the Approve
button knows where to send: **X** `dm_conversation_id`, **Instagram** the sender's IGSID (you message
the *person*, not a thread). **Reddit is the exception** — a PM reply is addressed by the message
fullname (`t4_<id>`) itself, so `reply_to` stays empty and the destination derives from `target_id`.
`list_dms` normalizes every platform to `{id, conversation_id, sender, sender_id, text, created_at}`;
the read tools annotate `already_answered` from the `dm` ledger, and each platform's `list_dms`
**excludes the account's own outbound messages** (by `sender_id`/author) so the agent never answers
itself. New scopes gate this and need a **reconnect**: X `dm.read`/`dm.write`, Reddit
`privatemessages`, Instagram `instagram_manage_messages` (App-Review gated, so it lives in
`OPTIONAL_SCOPES` — an app without approval strips it via `INSTAGRAM_SCOPES` rather than failing the
whole dialog). All three platforms only permit answering a message someone sent you, within their
messaging window — never an unsolicited DM; the prompts say so.

**Comments live PER POST on Instagram, so an engage run must sweep every recent post, not just the
latest.** `instagram_comments` reads ONE `media_id`; a run that only checked the newest post left the
comment on a reel unanswered. `instagram_recent_comments` walks the recent media (feed AND reels) and
returns all their comments together, each tagged with its `media_id`/`media_type` and
`already_answered`, so one run answers them all — one post failing is skipped, never fatal. The
ENGAGE and AUTO prompts both say to work through every new comment on every post. On X, `x_replies`
already spans recent posts by `conversation_id`.

**Liking is X-only, and it is NOT gated.** `Capabilities.supports_liking` + `SocialPlatform.
like_target(access_token, account, *, target_type, target_id, like=True)` (base raises); only X
implements it (`POST /2/users/:id/likes` / `DELETE …/likes/:id`), needing the **`like.write`** scope
— an account connected before it was added must be **reconnected**. Instagram's Graph API, YouTube's
Data API and TikTok's app API expose **no** like-a-comment endpoint, so they declare
`supports_liking=False` and offer no like tool. Liking is an immediate write like moderation/delete
(a like is not outbound *content*), idempotent so it needs no ledger, and does not count as
answering — a liked comment can still get a reply. `x_like_post(post_id, like=True)` can also un-like.
`scripts/preflight.py` + `tests/test_store_interface.py` bind `like_target` against every
`supports_liking` platform, the same drift guard as `publish`/`reply_to_target`.

## Gotchas

- **The brand mark is a PATH, not text** (`static/brand/`, geometry in `brand/_glyph.txt`). A
  favicon drawn with `<text font-family="Space Grotesk">` falls back to whatever the viewer has
  installed, so the mark would differ per machine; the A is drawn as two subpaths with `evenodd` so
  the counter is a hole. The rasters (`favicon.ico`, `favicon-32.png`, `apple-touch-icon.png`) are
  generated by `scripts/make_brand_assets.py` from the SAME numbers — never hand-edit them, or the
  SVG and the PNG drift apart. The badge's border is always the SURFACE colour (that is what
  separates it from the A), so `mark-dark.svg` strokes with the design's ink and `mark-dark-sm.svg`
  with the dashboard's `--panel`. The "2" is dropped below ~48px, per the design. Wordmark type uses
  `textLength` + `lengthAdjust`: laid out by hand it overflowed its card as soon as a different
  fallback font rendered it.
- **`--brand-accent` is for the MARK; `--accent` is the UI.** They are deliberately different.
  `#E85C7A` sits 16 units from `--danger` (`#f0616d`) in RGB — the same colour to a human — so a
  primary button and a Delete button in it were nearly indistinguishable, which is a misclick
  hazard, not a taste question. Interactive stays blue (`#6ea8fe`), red keeps meaning destructive,
  and `var(--brand-accent)` appears in exactly ONE rule (`.brand-word sup`). A test asserts that
  count, the RGB distance from `--danger`, and the contrast of `--accent` on both panels.
- **The mark and the wordmark are ALTERNATIVES, never shown together.** The mark is an A, so beside
  "AISM²" it reads as a stray letter. `.brand-mark` is `display: none` until the 420px block swaps
  the two — one or the other at every width.
- **Dashboard mobile rules** ([static/style.css](aismm/dashboard/static/style.css)): a new `<table>`
  must be wrapped in `<div class="table-scroll">` or the whole page scrolls sideways on a phone, and
  form controls must stay **16px on touch** (`@media (pointer: coarse)`) or iOS Safari zooms on focus
  and never returns — a template using a new `input[type=…]` must add it to all three
  `.form input[type=…]` rule lists, since they enumerate types rather than matching every input.
  `tests/test_responsive.py` enforces both; it also covers the viewport tag, the scrollable nav, and
  44px tap targets.
- **`describe_image` must NEVER proof-read our own generated image.** It reads text
  approximately and is worst at exactly what is worth checking — phone numbers, non-Latin scripts,
  RTL, small print. A live run failed because it read a *correct* Persian footer as garbled and a
  *correct* phone number as malformed: a verifier less reliable than the thing it verifies vetoes
  good work, burns a second generation, and can fail the run. The tool docstring and the prompt both
  forbid it now; the earlier wording ("checking a generated image came out as asked") is what invited
  it. The prompt also says the report_failure cases are failures of **input** — the agent must not
  invent acceptance tests of its own **output** and fail on them; when unsure, publish through the
  instruction's gate and let the approval queue decide.
- **Preview media is `object-fit: contain`, thumbnails are `cover`.** A 9:16 reel cropped into a
  4:3 box means reviewing a post whose edges you cannot see — so anything being *judged*
  (`.staged-media`, `.detail-media`) letterboxes, while a 52px thumbnail may crop because it is a
  glance, not the thing being judged. `<video>` needs **`playsinline`** or iOS Safari takes the whole
  screen to play, and `/assets/<file>?download=1` sets `as_attachment` because iOS cannot save a
  playing video any other way — the bare URL must stay inline, since Instagram fetches it.
- **`.staged-list` uses `auto-fit`, not `auto-fill`** — `auto-fill` leaves empty tracks beside a
  single card, which is what made one staged post sit in a narrow column with a gap next to it. The
  max track width (380px) stops that one card stretching across a wide screen instead, and the list
  scrolls at 70vh rather than pushing the page down.
- **Platform brand marks come from vendored Simple Icons SVGs**
  ([dashboard/platform_icons.py](aismm/dashboard/platform_icons.py), files in
  `static/brand/platforms/` + a NOTICE recording CC0 and the download URL). The instruction list
  shows them beside the account count, because "3 accounts" doesn't say WHERE it posts. The path is
  read out of the SVG at import, so the file is the single definition — never paste a copy into
  Python. They are INLINED, not `<img>`: a row would otherwise fire four requests, and an `<img>`
  can't be recoloured. X and TikTok publish monochrome marks so they take `currentColor` (black
  would vanish on the dark dashboard); Instagram and YouTube keep their brand colour. **The enum
  member is `twitter` but the brand is `X`** — `FILES`/`COLORS` bridge that here rather than
  renaming an enum every DB row uses. An unknown platform renders nothing and a missing file is
  logged, never fatal; the count and the `title` still carry the information.
- **Instructions are filtered in the VIEW, runs in the STORE.** Deliberate asymmetry: the run table
  grows without bound and must be paged in SQL, while an operator has tens of instructions. The
  media thumbnail belongs on the RUNS list — a run has an asset, an instruction does not.
- **Never put `display:flex` on a `<td>`** — it drops the cell out of table layout and its contents
  overlap the neighbouring columns. Row actions are `<td class="actions-cell"><div class="actions">`;
  the cell is `width:1%` + `nowrap` so it shrinks to fit rather than being squeezed by the data
  columns (adding the third *Clear cooldown* button wrapped each one onto its own line before this).
- **`.form label` sets `flex-direction: column`** and, at (0,0,1,1), outranks any lone class — so a
  row-shaped control inside a form needs `.form label.my-class`, not `.my-class`. This is why every
  checkbox in the tool picker stacked on top of its own name at first; `.check` solves the same
  problem with `!important`.
- **`[hidden] { display: none !important; }` is load-bearing** ([static/style.css](aismm/dashboard/static/style.css),
  the first rule in the file). `hidden` is only a USER-AGENT rule, so ANY author `display`
  declaration beats it — and this stylesheet sets `display` on nearly every wrapper a conditional
  control uses (`.form label`, `.field`, `.check-group`, `.btn`, `.info-tip`,
  `.multiselect-option`). Without that one line the attribute is INERT: a YouTube visibility picker
  sat on an Instagram-only instruction, the tool filter hid nothing, and the `+`/`×` row buttons
  showed up with scripting off. Nothing in the toggling was ever broken. Every progressive-
  enhancement control here renders `hidden` and is revealed by script, so removing it breaks the
  whole dashboard's conditional UI at once; a test asserts the rule exists.
- **A control that cannot act is HIDDEN, not just ignored — on BOTH axes, task and platform.**
  YouTube visibility and the X community card sat on an Instagram-only instruction; a Reply policy
  sat on a pure publishing one — decisions recorded that could never take effect. Two predicates
  gate the instruction form and are ANDed: the **task** (`data-publish-only` / `data-engage-only` /
  `data-outreach-only`, mirroring `registry.always_on_for`) and the **platform**
  (`data-platform="youtube|twitter"`, from `dashboard/app._selected_platforms`). Both are rendered
  **server-side** so the first paint is already right, and ONE `sync()` owns `el.hidden` — two
  handlers each assigning it is last-writer-wins, not AND, which is exactly how changing the Task
  select used to bring the platform fields back. Two rules keep it safe: **`data-platform-keep` on
  a field that already HOLDS a value**, so a setting in effect never becomes invisible, and hidden
  controls **still submit** — hiding must not wipe a saved value when an operator edits something
  unrelated. Same rule outside that form: the tool picker hides another platform's tool GROUP
  (`TOOL_GROUPS` carries the platform; the factories return `None` off-platform anyway), and
  "Also share with followers" appears with the first X community ID *typed*, not only after a save.
  Bulk **Select all/Clear** compose with the filter but ignore the platform hiding — an
  off-platform tool left ticked is still submitted and stored.
- **A "one big string" field gets a ROW EDITOR, and the parser stays the one place that knows the
  grammar** ([static/repeat.js](aismm/dashboard/static/repeat.js), `data-repeat` /
  `data-repeat-row` / `data-repeat-add`). X communities were `ID = Name`, one per line, commas
  meaning something else; outreach targets carry their kind in a sigil (`#`/`r/`/`@`); the Sora pool
  was three comma-separated lists that had to line up by position. Each is now one thing per row
  with its own inputs, and the route rebuilds the stored text (`_community_entries`,
  `_engagement_targets`, `_sora_rows`) — `twitter.parse_community_entries` and
  `targets.parse_targets` are still the only grammar definitions. **The free-text field is still
  accepted when no rows are posted** (a script or bookmarked POST keeps working), one blank row is
  always rendered server-side so a value can be added without scripting, and JS only adds the `+`/`×`
  (hence the markup renders them `hidden`). Cloning a row resets a `<select>` by `selectedIndex`
  (assigning `''` selects nothing) and blanks any `data-repeat-blank-placeholder` — a "stored"
  placeholder is a lie on a new row.
- **The Sora pool's three CSVs line up BY POSITION and the keys are never echoed back**, so they can
  only be replaced as a SET. `save_provider_config` refuses a partial fill ("typing one means typing
  them all") and refuses blank keys when the endpoint list changed, because either would silently
  send one resource's key to another. Keying "keep this one" on the endpoint URL would mean reading
  plaintext secrets in a route — `_decrypt_provider_secrets` is private and stays that way.
- **Sharing names people by TYPED email.** The old control was a multi-select populated from the
  people this deployment had already seen — on a fresh install, nobody: the form rendered with no
  options, "Update sharing" posted an empty body, and it flashed "Sharing updated." A button that
  cannot do anything is worse than no button. Now each existing share is a row with a hidden
  `shared_with` (removing the row is what stops the sharing), plus one `share_add` box backed by a
  `<datalist>`; a rejected address is reported rather than dropped, and the flash says the resulting
  state (`_sharing_message`), not "updated". With **SSO off** the page says sharing needs sign-in
  instead of rendering an inert form — there is one implicit local operator who owns everything.
- **Every column heading on /instructions sorts** (`INSTRUCTION_SORTS` + the `sort_header` macro in
  `templates/_macros.html`). One key per column, each a **tuple ending in `name.lower()`** so equal
  values (three paused instructions, four in `dry_run`) keep a stable order instead of shuffling per
  request. `created_at` has no column but stays a valid key — an unknown key silently rewrites to
  `name`, so dropping one would break existing links. Each page passes its own filter-preserving URL
  builder to the macro; `instructions_url` **drops unknown overrides** (the shared macro also resets
  `page`, and this list is not paged).
- **A timestamp gets a relative second line, never a replacement** (`dashboard/humanize.time_until`,
  the `time_until` template global). "in 3 minutes" is the answer to what a *Next run* column is
  actually asked; the absolute UTC value is what you cross-check against the service log, so both
  are shown. It returns `""` for anything it cannot read rather than guessing.
- **Per-instruction tool selection** (`Instruction.tools_json`, `registry.build_tools(state,
  enabled)`). Empty list = ALL, so a newly registered tool is available to instructions that never
  narrowed their choice; `registry.ALWAYS_ON` (`publish`, `report_failure`) is never withheld or a
  run could not end. `dashboard/app._selected_tools` must keep "nothing ticked" distinct from
  "everything ticked" — both look like an empty list, and collapsing them would silently re-enable
  every tool. The form posts a `tools_present` marker so a POST without the picker leaves the stored
  selection alone instead of resetting it.
- **Retry re-runs with a prompt override** (`orchestrator.retry_run` →
  `run_for_account(..., prompt_override)`). It creates a NEW run — the failed one is the evidence —
  and the override is used **verbatim** (`prompt_override.strip() or build_kickoff(...)`), so memory
  and the note are deliberately NOT re-inlined: what the operator reads in the box is what the agent
  gets. Everything else still applies (publish gate, lock, cooldown, duplicate guard).
- **Republish is the OTHER repair, and usually the right one** (`orchestrator.republish_run` →
  `perform_publish` directly, no agent). Most failures are a refused *publish*, not bad content —
  rate limit, expired token, X out of credits — and re-running the agent for that regenerates the
  media, spends money and produces something *different* from what was reviewed. So `Run` records
  `asset_paths_json` + `placement` (written by `perform_publish`, not by the agent) and republish
  replays them verbatim with an editable caption. It creates a NEW run like retry, checks
  `asset_exists` for every path first (`media_gone` beats publishing whatever is at that path now),
  and skips the cooldown check for non-`live` modes since they call no API. The run-detail page
  shows republish `btn-primary` and open, re-run secondary — the cheap fix must be the obvious one.

- **Widening a table is now safe**: `LocalStore._add_missing_columns` runs `ALTER TABLE ADD COLUMN`
  for any column the models declare and the DB lacks (Azure Table is schemaless, so it needs
  nothing). Prefer a real column over a side table for new per-instruction fields; `InstructionState`
  stays a side table because memory/note are large, mutable and semantically separate.
- **`,` combines, `;` separates** ([schedules.py](aismm/schedules.py)) — the difference between 6
  posts a week and 3. `03:00 thu, 03:00 tue, 15:00 sun` is ONE cron whose hours and days are
  cross-multiplied (03:00 *and* 15:00, on all three days); with `;` it is three separate triggers.
  `describe()` dedupes the hours (reading back "at 03:00 and 03:00" looks like a bug rather than a
  cross-product) and appends "N× a week" whenever a part has more than one time of day, which is the
  only thing that makes the multiplication visible. Steps and unparsed day fields are NOT counted —
  a wrong count is worse than none. The instruction form carries the full cheat-sheet in a
  `<details>`; keep it in step with what the parser accepts.
- **One schedule → SEVERAL triggers** ([schedules.py](aismm/schedules.py)): `parse_schedule` returns a
  list and `refresh_jobs` registers one job per trigger (`instr:<id>`, `instr:<id>:1`, …). Ambiguous
  input is REFUSED, not guessed — a bare `6` could be 06:00 or every 6h. `describe()` is the
  dashboard readback; keep it defensive, cron fields can be `*/4`.
- **Interval triggers MUST be given an `anchor`** (`parse_schedule(..., anchor=...)`). An
  `IntervalTrigger` with no `start_date` anchors to the moment it is *constructed*, and `refresh_jobs`
  reconstructs every trigger on every service restart **and every dashboard save of any
  instruction** — so `every 1h` silently pushed its next fire an hour out each time, with nothing in
  the log. The anchor is `instruction.schedule_start_at or instruction.created_at`, so phase survives
  a rebuild. Cron parts don't drift; there the anchor only gates "don't fire before". Note
  `CronTrigger.from_crontab` takes **no** `start_date` — use `_cron_from_crontab`.
  `scheduler.next_run_for(id)` is the raw next fire (live scheduler state, `None` when the scheduler
  isn't running in this process); `next_run_after(id, when)` asks the triggers for the first fire at
  or after a moment, which the dashboard needs because the next *fire* is not the next *post*.
- **"Next run" in the UI must mirror `_run_one`'s skip rule** (`dashboard/app._next_run_info`). A
  `live` run whose account is in a cooldown is skipped before doing any work, so showing the raw
  fire time promised a run that the log then reported as "Skipping … rate-limited". Only `live` mode
  and only when EVERY target account is blocked — one free account still makes the fire worth
  firing, and `dry_run` calls no API so it never skips. Keep this in step with the orchestrator if
  that condition changes.
- **gpt-image-2 ≠ gpt-image-1** ([tools/image_tool.py](aismm/tools/image_tool.py)): image-2 takes
  arbitrary sizes (edges ×16, ratio ≤3:1), **rejects `input_fidelity` outright**, and has no
  transparent background; image-1 accepts only three sizes but supports both. `resolve_size` fixes a
  size instead of letting the API fail with an unexplained error.
- **YouTube visibility is per instruction** (`Instruction.youtube_privacy` → `youtube.
  resolve_privacy`; `""` inherits `settings.youtube_privacy`, env `YOUTUBE_PRIVACY`). It used to be
  a bare `os.getenv` inside the platform — deployment-wide, and invisible to the tests that pin the
  environment, which is exactly what the "no `os.getenv` outside config.py" rule exists to stop.
  **An API project that has not passed YouTube's compliance audit has EVERY upload locked to
  private** whatever is requested, and the lock cannot be appealed (the video must be re-uploaded
  through an audited client) — so `publish` compares the requested `privacyStatus` with the one in
  the response and puts the difference in `PublishResult.raw["notice"]`. `publish_tool` appends any
  `notice` to `run.log` and returns it to the agent: a clean "published" over a silently private
  video is the worst outcome. Absence of `status` in the response is NOT evidence of a downgrade.
- **AI disclosure is per instruction too** (`Instruction.disclose_ai` checkbox). The global
  `AI_DISCLOSURE_ENABLED` is the master — an instruction may opt out below it, never back on.

- **A failed shot is SKIPPED, not the end of the sequence** (`_MAX_SHOT_FAILURES`). The loop used to
  `break` on the first failure, which turned one transient Sora error into "only 1 of 9 shots
  rendered" — eight shots never even attempted, and a 12s stub of a 45s trailer. Shots are
  independent clips, so a failure is a gap; only a systemic problem (3 failures: dead resource, no
  credits) stops it. `failed_shots` names them and the `warning` tells the agent the merged clips
  are still usable.
- **Multi-clip video** ([tools/sequence_tool.py](aismm/tools/sequence_tool.py) +
  [video.py](aismm/video.py)): Sora renders 4/8/12s, so longer videos are merged with
  imageio-ffmpeg's bundled binary. Three consistency levers, all applied together because GenBox
  applying one at a time still drifts: the `style` block is repeated in EVERY prompt, the reference
  frame is explicitly described as the previous shot's final frame, and the sequence is **pinned to
  one Sora resource** (remix is job-scoped — GenBox's per-clip failover destroyed it). Always
  re-encode before concat and add silence to mute clips, or the merged file loses audio from the
  first silent clip onward.
- **A sequence is directed PER SHOT, not by one setting** ([tools/sequence_tool.py](aismm/tools/sequence_tool.py)):
  `scene_seconds` (length), `scene_continuity` (`""` inherits, `"cut"`, `"remix"`) and
  `reference_asset_paths` (one image per shot). One mode for a whole sequence is what produced
  "gaps and repeats": every shot was told to CONTINUE the last one, so a jump to a new place came
  out as another take of the same beat. `"cut"` gets its own prompt contract — *same film, new
  shot* — and is never remixed, since remix means "the previous shot, advanced". The tail frame is
  still extracted across a cut so a later shot can chain again.
- **A generated image is refused by Sora exactly like any other — painting frames for video is
  wasted money.** `input_reference` is rejected whenever it contains a human face, and *who drew it
  is irrelevant*: a gpt-image-2 frame made specifically to seed a shot comes straight back. An
  earlier version of this file told the agent to build a character sheet and paint the opening frame
  of every cut; that advice is REMOVED from the prompt, `create_video_sequence`'s docstring and
  `generate_image`'s docstring, because it spent a generation per cut on images that never reached
  the model. With people on camera exactly two levers remain, and they are used together: `style`
  repeated verbatim in every shot (identity), and **remix** (continuity). Reference images stay
  useful for material with NO people in it — locations, objects, artwork, landscapes.
- **`continuity="auto"` switches the REST of the sequence to remix after one refusal**
  (`frames_refused` in [tools/sequence_tool.py](aismm/tools/sequence_tool.py)). The refusal is proof
  this material has faces in it, so re-offering a frame on every remaining shot buys nothing and
  costs a failed create each time; tail-frame extraction stops too. One shot pays for the discovery.
- **EVERY chained shot is a remix, cuts included, and its SOURCE is a per-shot choice**
  (`continuity` defaults to `"remix"`; `scene_remix_from`). Remix is the only continuity lever that
  survives the face rule, so it is not reserved for "continuing" shots: on a **cut** the source
  fixes the LOOK and `build_clip_prompt`'s cut wording asks for a new moment ("the video you are
  editing is shot N … ONLY to fix the look"). Treating a cut as *no source* is what let the cast
  change across a jump. `scene_remix_from` is a SEPARATE axis from `scene_continuity` — that one
  says what the prompt asks for, this one says where the pixels come from. Default `0` = the
  previous shot (the action advances), but every forward link drifts further, so a shot returning
  to the opening framing names shot 1: `[0, 0, 1, 0, 1]`. **Forward for continuity, back for
  recall.** An impossible source (forward reference, failed shot) falls back to the previous shot
  and says so in `timing_notes` rather than costing the clip. A cut is only remixed when the
  sequence is chaining at all — `continuity="none"` chains nothing.
- **A remix inherits its source's duration, and that is ACCEPTED, not worked around.** An earlier
  version rendered a shot fresh to honour its `scene_seconds`; that throws away the only continuity
  lever there is. So the video is **n × `seconds_each`** — 3 shots at 12s IS 36 seconds — and
  varying `scene_seconds` under a remix mode produces ONE up-front `timing_notes` entry rather than
  per-shot surprises after the money is spent.
- **Pacing is fixed in the WRITING, not in the lengths** (`plan_shot_timing` →
  `estimate_speech_seconds` + `_FILL_FLOOR`/`_FILL_CEILING`). Since the length is fixed, the two
  reported failures are both authoring errors: `over` (dialogue overruns → cut off mid-sentence)
  and `under` (dead air at the back of the shot). The target is a **band, not a number** — writing
  to exactly the clip length is what breaks a sentence when the model delivers it slower than the
  arithmetic predicts, so the margin IS the feature. Deterministic arithmetic, hence a tool rather
  than prompt advice; the prompt's job is to say every cut lands on a clip boundary and a scene
  change inside a clip is described in that shot's own prompt.
- **Shots render ONE AT A TIME on ONE resource; the pool balances between runs, not within one.**
  Shot N+1 may need to remix N or chain from its final frame, so it cannot start before N finishes,
  and a job id only exists on the resource that created it — `create_clip_with_failover` is used for
  shot 1 alone, and everything after it goes through `create_clip(resource, …)` on the pinned
  resource. Never parallelise the shot loop or add mid-sequence failover; the round-robin cursor
  advancing per run is what spreads load.
- **A supplied image is a LOOK, not a paused video** (`from_supplied_image` in `build_clip_prompt`).
  The continuity wording tells Sora to *resume* from the frame; applied to a panel the operator
  chose, that asks it to continue an action that never happened. A supplied image wins over the
  chained frame at that index — naming a panel for a shot is more specific than "continue" — which
  also means **a shot with its own picture is not chained at all**, so giving every shot a reference
  silently opts the whole video out of remix and `scene_remix_from` never applies. A seven-shot
  trailer planned `[0, 1, 2, 2, 4, 5, 6]` and remixed nothing. That case is now a `timing_notes`
  entry naming the unchained shots.
- **A refused reference falls back to the sequence's own continuity, never to nothing.** The
  earlier rule — retry that shot WITHOUT the image, since a remix "would quietly answer a different
  request" — is REVERSED: the picture is out of play either way (Sora will not look at it), so the
  only question left is what anchors the shot, and any anchor from this sequence beats none. On the
  trailer above, shots 2 and 6 were the only unanchored ones and the only two whose cast changed.
  `perform_create_sequence` now walks three rungs and records which one it used in `how`: **remix an
  earlier shot** (`remix(shot N, image refused)`), else **borrow a picture Sora has already
  ACCEPTED** in this run (`accepted_seeds` → `create+image(shot N's, image refused)`; a refusal is
  about the picture, not the account, so one that already went through is known usable), else the
  prompt and `style` alone (`create(image refused)`), which is reported as `stranded`.
- **Pin the CAST, let the story move** (`_CAST_CONTRACT`, on every shot of a multi-shot video). The
  continuity clause used to order "keep its subject, wardrobe, LOCATION, LIGHTING and framing
  exactly" and the scene below it would ask for twilight on a hill in the rain — a prompt at war
  with itself. A model resolves that by regenerating, and what it regenerates is the *characters*: a
  five-shot children's animation whose remix chain was completely intact still ended with different
  animals than it opened with. So the invariant (characters, designs, wardrobe, colours, art style)
  is stated separately and absolutely, while place, time of day, light and framing explicitly follow
  the shot. The contract goes on the first shot and on a shot whose chain broke too — a clip
  rendered from the prompt alone is exactly where the cast is most likely to change.
- **Every forward link is another generation away from shot 1** (`_CHAIN_DRIFT_LINKS`). `[0, 0, 0,
  0, 0]` is a legal chain and a drifting one; past three consecutive `remix(previous)` links
  `timing_notes` says so and suggests anchoring the later shots back to an early shot (`[0, 0, 1, 1,
  1]`). Reported rather than rewritten — where to re-anchor is a directing decision.
- **`seconds_each` defaults to 12, deliberately.** The agent was picking 4s clips and the result read
  as a slideshow; 12s is fewer seams, less drift and room for the action to move. Per-shot lengths
  are the rhythm lever, not the default.
- **Sora refuses `input_reference` containing human faces, so IDENTITY lives in `style`.** A single
  seed whose face was not visible let Sora invent a different character entirely. `style` is repeated
  verbatim in every prompt and survives a refusal, so the character description belongs there; the
  result reports `reference_images_used` vs `..._given` plus `reference_notes` naming the refused
  shots, which are exactly the ones now depending on `style`. A collage of panels is NOT a fix:
  `input_reference` is a starting frame, so a collage yields a video of a collage, and compositing
  several panels makes a visible face — and thus a refusal — more likely, not less.
- **A remix chains from the PREVIOUS shot, never from shot 1.** Anchoring every fallback remix to
  shot 1 was the original design and it published a reel whose opening moment played three times:
  each later shot applied its own prompt to the same untouched starting point, so nothing advanced.
  `input_reference` is refused for *every* shot once people are on camera, so this path is the
  common case, not the edge case. Chained drift is the lesser evil; repetition is not a video.
- **A remix inherits its source clip's duration** — `remix_video_job` takes only a prompt. A shot
  asking for 8s that falls back to remixing a 4s clip renders **4s**, which is how a 4/4/4/8 plan
  shipped as 16s while reporting 20s. Every clip is measured after the fact (`video.duration_seconds`
  per clip); `shots[].seconds` is the REAL length, `requested_seconds` appears only when they differ,
  and `warning` tells the agent to caption the real duration. Never report the requested value.
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
- **Reasoning models REJECT `temperature`, so every agent builds settings through
  `llm.agent_model_settings`** — never `ModelSettings(...)` directly (a test asserts no module
  outside `llm.py` does). Repointing `AZURE_OPENAI_MODEL` at `gpt-5.6-luna` 400'd every run with
  "Unsupported parameter: 'temperature' is not supported with this model": o1/o3/o4 and gpt-5.x do
  their own sampling and refuse the parameter rather than ignoring it. `supports_sampling` guesses
  from the model name, which on Azure is the operator-chosen DEPLOYMENT name — so
  `LLM_SUPPORTS_TEMPERATURE=0|1` overrides it in both directions (tri-state: unset ≠ False).
  `None` is the right way to drop a setting; the SDK turns it into `omit` and the key never reaches
  the wire.
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
  **Some content does not exist until you CLICK it**, so `browse_page` takes a `click` selector and
  returns a `buttons` list. A comic page kept its character sheet in a modal whose `<img>` had no
  `src` attribute at all until a button set it — unreachable by any amount of waiting — and the
  control was a `<button>`, so it wasn't in `links` either: the agent could neither see the image nor
  discover the thing that reveals it, and correctly reported it couldn't finish. `buttons` is the
  discovery path; keep it in the tool docstring and the prompt.
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
- **Publish quality is deliberately high, and measured** ([media.py](aismm/media.py)). Three settings
  were quietly softening every post: a first pass at q88 while using 3% of Instagram's 8 MB budget,
  `subsampling="4:2:0"` (invisible on photos, brutal on the line art and lettering this app posts),
  and Pillow's default *bicubic* for what is always a DOWNscale. Now q95 + full chroma (`subsampling=0`)
  + LANCZOS, backing off only when a byte cap bites. On a line-art panel that moved PSNR 24.5 → 26.6 dB,
  where **26.60 dB is the ceiling set by the 1536→1440 resize itself** — i.e. the encode is now
  essentially free. Keep `tests/test_media_conversion.py`'s ceiling test: it is what stops the next
  "just lower quality a bit to save bytes" change.
- **`optimize=True` needs `ImageFile.MAXBLOCK` raised** at these qualities, or Pillow raises "broken
  data stream when writing image file" on a dense image. Sized to the image, with a no-optimize
  fallback.
- **`normalize_image` returns the input UNTOUCHED when it already complies** (`_already_compliant`).
  Re-encoding a JPEG that needs nothing is a free generation of loss, and publishing the same asset
  twice used to degrade it twice.
- **Ratio fit runs BEFORE the width clamp.** Padding widens the image, so clamping first let a
  1000×2000 come out 1616 wide — past the very `max_image_width` just applied.
- **Images are normalized before publishing** ([media.py](aismm/media.py), called from
  `perform_publish` so preview/approval/live share one converted file). Platform limits live on
  `Capabilities` (`image_formats`, `max_image_bytes`, `min/max_image_ratio`, `max_image_width`);
  only Instagram declares them today — **JPEG only**, 8MB, 4:5–1.91:1, ≤1440px — and anything else
  comes back as a misleading "Media download has failed". Flatten alpha before JPEG (Pillow raises
  otherwise), and **pad, never crop**, to fix a ratio. Conversion is best-effort: on failure pass
  the original through so the platform's error surfaces. Video isn't re-encoded (no ffmpeg dep).
- **A STORY is 9:16; the 4:5 floor is a FEED limit.** `Capabilities` carries `story_min_image_ratio`
  / `story_max_image_ratio` and `_normalize_image_for` takes the `placement`, because padding a
  correct 1080×1920 story up to 0.8 published it pillarboxed — succeeded, looked broken. A platform
  that declares no story limits falls back to the feed ones.
- **There is NO music/audio parameter in the Content Publishing API.** A container takes
  `image_url`/`video_url`, `media_type`, `caption`, `is_carousel_item`, `upload_type`,
  `is_ai_generated` and branded-content fields — nothing for Instagram's audio library. Audio can
  only arrive already inside the video file (Sora clips carry their own; `video.concat_clips`
  preserves it). Don't add a "music" argument; it cannot be implemented, and the publish docstring
  says so to stop the agent claiming a soundtrack in a caption.
- **Instagram placements** ([platforms/instagram.py](aismm/platforms/instagram.py)): carousel =
  child containers with `is_carousel_item=true` and NO caption, then a `CAROUSEL` parent carrying the
  caption + `children=<ids>`; a video child is `media_type=VIDEO` (**not** `REELS`, which is
  standalone); a story is `media_type=STORIES` and takes no caption. `publish` validates placements
  against `Capabilities` (`supports_carousel`/`supports_stories`/`max_carousel_items`) before calling
  the platform.
- **Volume refusals are not content errors** ([platforms/instagram.py](aismm/platforms/instagram.py)
  `RateLimited`, codes 4/17/32 + subcode 2207051 "action is blocked"). Never retry one: Meta extends
  blocks when an app keeps knocking. `perform_publish` starts a per-account
  [cooldown](aismm/cooldown.py) (stored in `account.meta`, so no schema change and both backends
  persist it) and `orchestrator._run_one` skips later **live** runs for that account before doing any
  work — `dry_run` still proceeds since it calls no API.
- **The cooldown ESCALATES on repeated refusals** (`cooldown.start`, doubling from the base, capped
  at `MAX_COOLDOWN_SECONDS` = 24h). A flat 60-minute cooldown against an `every 1h` schedule is close
  to useless if whatever actually triggered the block outlasts an hour: the next scheduled fire lands
  right as the cooldown clears and knocks again, which — per the point above — extends the real
  block. Each `RateLimited` bumps a strike counter (`STRIKES_KEY`, next to the deadline in
  `account.meta`); a clean, non-reconciled publish resets it via `cooldown.clear`, so one bad hour
  doesn't escalate every isolated refusal for the rest of the account's life. **A strike counts
  failing to publish, never publishing with a noisy error** — a *reconciled* publish (the post
  landed despite the 403) calls `start(..., escalate=False)`: base cooldown, no strike. Counting
  those drove a perfectly healthy account 60 → 120 → 240 minutes across four consecutive
  *successful* posts, throttling it toward silence. A landed post neither resets nor advances the
  streak: it isn't a failure, but the error was real, so the next genuine refusal resumes from where
  the streak was. The dashboard's
  **Clear cooldown** button calls `cooldown.clear(..., reset_strikes=False)` — a human override is
  not evidence the platform stopped blocking, and resetting the streak there would let a
  clear-then-refused loop restart at the base duration forever. A **reconciled**
  publish (the post landed despite the error) does NOT reset the streak — Meta is still signalling a
  limit even though this one got through.
- **The agent must write memory AFTER publish returns**, not before. The prompt used to say
  memory-then-publish, so a rate-limited run advanced its position past a panel it never posted and
  the next run skipped it. Steps 8/9/10 are attempt → publish → outcome; keep that order.
- **"Was it published?" is recorded in CODE, not in the memory**
  ([publish_ledger.py](aismm/publish_ledger.py)). The prompt above is necessary but not sufficient:
  a run wrote "attempting panel X", published successfully, then ended *without* the step-10 write,
  so the next run re-posted the same panel — two identical live posts. Every successful live publish
  now fingerprints its media (sha256 of the bytes + placement, in `account.meta` like the cooldown)
  and `perform_publish` **refuses** a fingerprint already in the ledger. Same reasoning as the AI
  disclosure and the publish gate: a guarantee that must hold on every path cannot live in
  model-written prose. Key on the MEDIA, never the caption — the agent rewrites the caption each run.
- **The ACCOUNT is the authority on what is published, not the ledger.** The ledger records what we
  posted; it cannot know a human deleted a post by hand, and a stale entry would make that content
  unpublishable forever. So a ledger hit is verified against the platform before it refuses
  (`publish_tool._confirm_duplicate` → `SocialPlatform.post_exists`). Gone → `forget` the entry and
  publish. **Archived counts as gone**, which needs TWO Graph calls because there is no
  `is_archived` field: `GET /{media-id}` (a *deleted* id answers code 803 / subcode 33; an archived
  one still resolves) plus `GET /{ig-user-id}/media`, whose listing **excludes archived posts**.
  Absence from that listing only means archived when the post is newer than the oldest row scanned —
  otherwise paging simply never reached its date, and the answer is `None`, not `False`. Only the rare refusal path pays for the call. `None` means *cannot tell* (no
  support, rate limited, network) and **publishes anyway** by default: for sequential content a
  wrongly skipped item breaks the running order permanently, while a duplicate is deletable.
  `PUBLISH_DUPLICATE_GUARD_STRICT=1` restores fail-closed. `_confirm_duplicate` returns
  `(index, entry, confirmed)` — keep the third element: a *confirmed* refusal tells the agent to
  `update_memory` and advance, an *unverified* one must tell it NOT to advance, or an item that
  never actually posted gets skipped out of the sequence for good.
- **One fingerprint PER ITEM, not per post** (`publish_ledger.fingerprints` + `find_any`). The first
  version hashed every item of a post into one combined digest, so a panel published alone and then
  published again as item 1 of a two-photo carousel produced two *different* digests and the guard
  never fired — it happened twice on the live account. The unit a follower sees repeated is the item,
  so a carousel is a duplicate if **any** of its items was already posted, and the refusal names
  which one. `MAX_ENTRIES` is sized for items, not posts.
- **A `media_publish` failure after the container is FINISHED means UNKNOWN, not failed**
  (`Instagram._find_recent_published`). Meta already holds the media at that point, and a code-4
  refusal on the last step can still have published. Treating it as failure left the position
  unchanged and the next run posted a duplicate — so we read back `/{ig-user-id}/media` and, if a
  post with our caption appeared in the last 15 minutes, report success with the real permalink.
  Needs a caption to match on: `/media` does not list stories, so a story failure is never
  reconciled (the ledger guards that case) or the newest feed post would be misread as ours.
  A reconciled publish reports success **and still starts the cooldown** — the post landed, but Meta
  signalled a limit, so the next run must not knock. [reconcile.py](aismm/reconcile.py) +
  `cli reconcile --apply` does the same repair retroactively for runs recorded before this existed
  (match failed runs to live posts by caption, fix the status, seed the ledger); it is read-only
  against the platform and dry-run by default.
- **The run lock is HEARTBEATED, and the TTL is short because of it**
  ([orchestrator.py](aismm/orchestrator.py) `_LockHeartbeat` + `Store.touch_lock`). The dashboard's
  "Run now" runs in a plain daemon thread, not a scheduler job — a gunicorn restart (`Restart=always`)
  kills it without unwinding `finally`, so its lock was never released and, at the old 30-minute TTL
  with no heartbeat, every scheduled run of that instruction was refused as "already running" by a run
  that no longer existed. Now a live run renews its lock every 60s and the TTL is 300s, so an orphaned
  lock clears in one TTL. Don't raise `_LOCK_TTL` to "allow longer runs" — that is what the heartbeat
  is for; the TTL only measures how long a *dead* owner blocks the next run.
- **A run is only closed by the process running it, so a restart strands the row**
  (`orchestrator.reap_stale_runs` + `close_stale_runs`). The wall-clock ceiling ends an overrunning
  run — but only while the process lives; a gunicorn restart, deploy or OOM kill mid-run leaves
  `status=running` forever and the Runs page fills with work nothing is doing. The heartbeated LOCK
  clears itself within one TTL, so the *instruction* recovers on its own; the RUN does not. Age is
  the signal and a safe one: a live run cannot outlast `RUN_TIMEOUT_SECONDS`, so past that plus a
  15-minute grace there is no process behind it (with the ceiling disabled it falls back to 24h).
  Swept at `scheduler.start()` — booting is exactly when they exist — plus a button on /runs and
  `cli runs [--apply]`. The sweep must never block the scheduler: its failure is logged and startup
  continues.
- **A run has a wall-clock ceiling** (`settings.run_timeout_seconds`, default 2h, env
  `RUN_TIMEOUT_SECONDS`; `0` disables it). APScheduler runs jobs in a bounded
  pool with `max_instances=1`, so one run that never returns silences its instruction *permanently*
  and leaks a pool thread; enough of them stop every instruction. The scheduler also logs
  `EVENT_JOB_MAX_INSTANCES`/`EVENT_JOB_MISSED` — a skipped fire used to be completely silent, which is
  why an instruction quietly not posting was so hard to diagnose. A `RUN START` with no matching
  `RUN DONE` is the signature.
- **Instagram engagement tools** ([tools/instagram_tools.py](aismm/tools/instagram_tools.py)) return
  `None` from their factories unless the run targets an Instagram account, so other platforms aren't
  handed them. Reply/moderate act on the live account IMMEDIATELY — they are not behind
  `publish_mode`, which gates posts. Insight metric names churn (v21 dropped `impressions`,
  `profile_views`, non-Reels `video_views`), so `DEFAULT_*_METRICS` stays minimal and overridable.
  Comments/insights need the `instagram_manage_comments` / `instagram_manage_insights` scopes — an
  account connected before those were added must be reconnected.
- **Publishing acts AS THE PAGE, so the stored token must be the PAGE token** — never the user
  token. `/me/accounts` only returns a page's `access_token` when the login actually granted page
  access; `fetch_identity` used to fall back to the user token when it didn't, which looked like a
  successful connect and then failed at publish time with `code=190 … must be granted before
  impersonating a user's page`. It now REFUSES the connection there, naming the page and the dialog
  step to redo.
- **Reconnecting must UPDATE the account row, never add one.** `upsert_account` keys on the row
  ID and the OAuth callback built a fresh `Account()` each time, so every reconnect duplicated
  every account that login covers — reconnecting ONE Instagram account produced a second copy of
  all three, because one Meta login claims every linked Page. The damage is not cosmetic: an
  `Instruction` stores account IDs, so its instructions kept pointing at the OLD rows, whose tokens
  the re-authorization had just invalidated — connected-looking account, configured-looking
  instruction, silently stopped publishing. The callback now matches on **(platform, external_id)
  within the workspace** and updates in place, MERGING meta (`{**kept, **new}`) so the X community
  list, `share_with_followers`, the publish ledger and the cooldown survive — none of them are
  re-derivable from an OAuth callback. A reconnect also **adopts the orphans of any duplicate it finds** — instructions still
  pointing at an older row are moved onto the one just re-authorized, so reconnecting repairs the
  damage instead of stepping around it. `/accounts/prune-duplicates` then removes the stale rows,
  repointing before deleting so an instruction whose only account row is about to disappear is not
  broken for good. `account_groups()` + `repoint_instructions()` are shared by the accounts page,
  the callback and the cleanup: three places used to decide which row survives, and if they ever
  disagreed one would repoint instructions at a row another was about to delete. **The survivor is
  the newest row** (last of the group, ordered by `created_at`) — it holds the token the newest
  authorization minted.
- **`REQUIRED_SCOPES` vs `SCOPE_FEATURES` — a missing optional scope is not a publishing failure.**
  The permission check compared the token against every scope the connect *asks* for, so a working
  X account reported "Token is MISSING dm.read, dm.write — that is why publishing fails". Neither
  has anything to do with publishing. Every platform now declares `REQUIRED_SCOPES` (empty = all of
  `scopes`) and `SCOPE_FEATURES` mapping each optional scope to the feature it powers, and the check
  reports three states: publishing broken (error), publishing fine but a feature is off (warning,
  named in words — "reading direct messages", not "dm.read"), or healthy. A test asserts every
  optional scope has a feature description.
- **One OAuth round-trip connects EVERY account it covers** (`SocialPlatform.fetch_identities`,
  default `[fetch_identity(...)]`; Instagram returns one Identity per linked Page, each with its own
  page token). This is the structural fix for the point below: if a single login claims all the
  Pages, there is never a second authorization to overwrite the first. The dashboard callback loops
  over the identities; a Page that came back without a token is skipped with a warning rather than
  failing the whole connect.
- **One Meta app + one Facebook user = ONE grant, and re-authorising REPLACES it.** Connecting a
  second Instagram account with only its own Page ticked strips `pages_show_list` /
  `pages_read_engagement` from the grant the FIRST account's page token was minted against.
  Observed live: three accounts on one app, the newest published fine while the two reconnected
  ones returned code 190 under identical code — the newest-works/older-break asymmetry is the
  signature. `dashboard/app._warn_about_collateral_damage` re-checks the other accounts after every
  connect and flashes a warning, because the breakage is otherwise invisible until the next run.
  **Accounts → Check permissions** works on EVERY platform:
  `SocialPlatform.inspect_token(access_token, account)` has a base implementation that proves the
  token by calling `fetch_identity` and reports the scopes recorded at connect
  (`meta["granted_scopes"]`, captured from the token response — most providers cannot be asked
  afterwards). Instagram overrides it with Graph `/debug_token`, which is the only way to tell a
  PAGE token from a USER one, and a USER token is the direct cause of code 190 however complete
  `scopes` looks. It was Instagram-only before, so the button answered
  `'Twitter' object has no attribute 'inspect_token'` on three of the four platforms it was offered
  for. Keep the messages platform-neutral — "tick this account's Page in the dialog" is Meta advice,
  and an account with NO recorded scopes must not be called healthy, only "the token works".
- **One unavailable scope kills the WHOLE OAuth dialog** ("Invalid Scopes: …"), so a review-gated
  analytics permission blocks publishing too — that is what `Invalid Scopes:
  instagram_manage_insights` was. `Instagram.scopes` is a property splitting `REQUIRED_SCOPES` (what
  publishing needs) from `OPTIONAL_SCOPES` (comments + insights, both App-Review gated).
  `DEFAULT_SCOPES` is currently all of them, so an app **without** insights approval cannot connect
  until `INSTAGRAM_SCOPES` strips it back — that env var replaces the list outright and is the
  documented escape hatch. If connect failures recur, the fix is to drop the review-gated scopes
  from the default, not to add retry logic: the refusal happens on Meta's page, before the callback,
  so AISMM never sees it.
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
  publishing. The post-login redirect must go through `_safe_next`, which **prefixes
  `request.script_root`**: Flask strips the reverse-proxy prefix before routing, so a remembered
  `request.full_path` sends everyone behind `/aismm` to `/instructions` after signing in. Authentication alone grants nothing: `AuthSettings.allows()` fails **closed** when the
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
