# AI Social Media Manager (AISMM)

An **autonomous, agent-driven framework** for publishing content to **Instagram, X (Twitter),
YouTube, and TikTok**. You connect accounts and write *instructions* (a brief + schedule + publish
mode); an [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) agent then researches
a topic, generates media (Sora 2 video / images), writes the caption, and publishes — on schedule,
with a code-enforced safety guardrail you control per instruction.

Built to mirror the proven patterns in the sibling **SandBox** projects (ComicBook's Agents-SDK
handoff/`@function_tool` design, GenBox's Sora 2 client), and designed to be **extended** — add a
new platform or a new tool with a single file.

---

## Table of contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration) · [Azure vs APIM](#azure-openai-or-apim) · [Sora / images](#media-generation-sora-2--images) · [env reference](#environment-variable-reference)
- [Connecting accounts](#connecting-accounts) · [Instagram](#instagram) · [X / Twitter](#x-twitter) · [YouTube](#youtube) · [TikTok](#tiktok)
- [Continuity: memory, notes, browsing](#continuity-memory-notes-and-browsing)
- [AI-content disclosure](#ai-content-disclosure)
- [Storage: local or Azure Table + Blob](#storage-local-sqlite-or-azure-table--blob)
- [The public-media-URL caveat (Instagram)](#the-public-media-url-caveat)
- [Running it](#running-it) · [VS Code debugging](#debugging-in-vs-code) · [Deploying (systemd)](#deploying-on-a-server-systemd)
- [Dashboard sign-in (SSO)](#dashboard-sign-in-sso)
- [Extending the framework](#extending-the-framework)
- [Project layout](#project-layout)
- [Security](#security) · [Caveats](#caveats--limitations) · [References](#references)

---

## How it works

Two concepts:

- **Account** — a social profile you connect via OAuth. Its tokens are stored **encrypted** at rest.
- **Instruction** — a directive you author in the dashboard: a free-form **brief** (persona / themes /
  goals), the **accounts** it targets, a **schedule**, a **publish mode**, and a media preference.

On each instruction's schedule (an APScheduler daemon), for every selected account, the agent runs
with **full autonomy**:

1. reads the brief + the platform's rules (`get_context`),
2. researches something real and current (`web_search`),
3. generates a **Sora 2 video** or an **image** if the platform/brief calls for it,
4. writes a platform-appropriate caption,
5. calls **`publish`** exactly once.

`publish` always runs, but the **publish mode** (set per instruction in the dashboard) decides what
actually happens — this is how "full autonomy" and "a safety switch" coexist:

| Publish mode | What happens |
|---|---|
| `dry_run` | Prepares a **preview** only. Nothing is sent to the platform. Review it under **Runs**. |
| `approval` | **Queues** the post. It publishes only when you click **Approve** in the dashboard. |
| `live` | Publishes **immediately** via the platform API. |

## Architecture

```
Instruction (dashboard-authored)  ──schedule──▶  APScheduler daemon
   • brief / persona                                   │  fires
   • target account_ids[]                              ▼
   • cron / interval                          orchestrator.run_instruction()
   • publish_mode (dry_run|live|approval)       for each account: single-flight lock
   • media_pref (auto|video|image|text)          └▶ Manager Agent (OpenAI Agents SDK)
                                                       tools: get_context, web_search,
Account (OAuth, tokens encrypted)                             generate_video (Sora 2),
   • instagram / twitter / youtube / tiktok                   generate_image, publish
                                                       Runner.run(high max_turns) + recovery
                                                    └▶ publish() gates on publish_mode:
                                                         dry_run  → StagedPost(preview)
                                                         approval → StagedPost(pending) ──▶ Approve ──▶ platform API
                                                         live     → platform API now
```

- **LLM**: Azure OpenAI **or** an APIM load balancer (one env toggle). Model shared across the agent.
- **Media**: Sora 2 video (direct REST, job-affinity resource pool) + gpt-image-1 images.
- **Storage**: local SQLite + Fernet-encrypted tokens, behind a `Store` interface (an Azure
  Table/Blob adapter can be dropped in — see `aismm/store/azure_store.py`).

---

## Quick start

```bash
cd /Users/abozar/Documents/Projects/aismm
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in at least the Azure OpenAI (or APIM) values
python -m aismm.cli run   # starts the scheduler + dashboard
```

Open **http://127.0.0.1:8787** → connect an account → create an instruction (start with
`publish_mode = dry_run`) → hit **Run now** → review the preview under **Runs**.

Smoke-test the plumbing without connecting any social account:

```bash
python scripts/smoke_llm.py    # verifies the Azure/APIM LLM wiring
python scripts/smoke_sora.py   # generates one Sora clip (skips if Sora isn't configured)
pytest -q                       # unit tests (publish gating, store, scheduler, platforms)
```

---

## Configuration

All configuration is via `.env` (loaded by `python-dotenv`). See `.env.example` for the annotated
full list; the essentials are below.

### Azure OpenAI **or** APIM

Pick one with `LLM_PROVIDER`:

```ini
LLM_PROVIDER=azure                # talk directly to Azure OpenAI
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_API_VERSION=2025-04-01-preview
AZURE_OPENAI_MODEL=gpt-4o         # your chat deployment name
```

```ini
LLM_PROVIDER=apim                 # talk through an Azure API Management gateway / balancer
APIM_BASE_URL=https://<apim>.azure-api.net/<openai-path>   # WITHOUT a trailing /openai
APIM_SUBSCRIPTION_KEY=...
APIM_KEY_HEADER=api-key           # or Ocp-Apim-Subscription-Key, per your APIM policy
APIM_API_VERSION=2025-04-01-preview
AZURE_OPENAI_MODEL=gpt-4o
```

> **`APIM_BASE_URL` is the gateway route *without* `/openai`.** APIM fronts Azure OpenAI, so it
> speaks the Azure URL shape and the client appends the rest: a base URL of
> `https://<apim>.azure-api.net/openailb` produces
> `https://<apim>.azure-api.net/openailb/openai/responses?api-version=…`. A value that already ends
> in `/openai` is accepted and de-duplicated. If your gateway returns 404s or timeouts, check the
> startup log line — it prints the exact URL calls go to.

Both paths build one shared `OpenAIResponsesModel` and register it as the SDK default (so the hosted
`WebSearchTool` routes through it too). APIM uses the **same `AsyncAzureOpenAI` client** as the
direct path (the trAIde pattern) precisely so the `/openai` segment and `api-key` header are right.
Wiring lives in [`aismm/llm.py`](aismm/llm.py); [`tests/test_llm_client.py`](tests/test_llm_client.py)
pins the request URL for both providers.

### Tracing (LangSmith)

```ini
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=AISMM
```

`configure_tracing()` routes the **Agents SDK's own tracer** into LangSmith with
`set_trace_processors([OpenAIAgentsTracingProcessor()])` — the same wiring as SandBox's ComicBook.
That is what produces the agent / tool / handoff span tree instead of a flat list of LLM calls. Don't
also add `wrap_openai` or `@traceable` around the same calls: SandBox's notes are explicit that it
duplicates traces and flattens the structure.

`langsmith` is a **hard dependency** (as in SandBox) — it's inert without `LANGCHAIN_API_KEY`, and
having it merely "optional" is how traces end up silently never appearing. If the log says
`LANGCHAIN_API_KEY is set but langsmith is not installed`, run `pip install -r requirements.txt`.

Without a LangSmith key the SDK would otherwise upload traces to `api.openai.com` using the **default
client's** key — which on Azure/APIM isn't a platform key, so every run logs
`Tracing client error 401: Incorrect API key provided`. In that case tracing is **disabled** instead.
Those 401s are noise, not a failed run, but they should no longer appear.

### Media generation (Sora 2 + images)

```ini
# Sora 2 video (comma-separated pools spread load; each job stays pinned to its resource —
# point these DIRECTLY at each resource, never at a round-robin gateway).
AZURE_OPENAI_ENDPOINT_SORA=https://<sora-a>.openai.azure.com,https://<sora-b>.openai.azure.com
AZURE_OPENAI_API_KEY_SORA=<key-for-a>,<key-for-b>
AZURE_OPENAI_MODEL_SORA=sora-2
AZURE_OPENAI_API_VERSION_SORA=preview
SORA_MAX_ATTEMPTS=0             # resources one clip may try; 0 = auto (pool size, max 3)

# Images (gpt-image-1) — a separate Azure resource, optional
AZURE_OPENAI_API_KEY_DALLE=...
AZURE_OPENAI_ENDPOINT_DALLE=https://<image-resource>.openai.azure.com
AZURE_OPENAI_MODEL_DALLE=gpt-image-1
```

Either can be left blank — the corresponding tool then disables itself and the agent works without it.

**Load balancing across several Sora resources** (same scheme as SandBox/GenBox). List the endpoints
and keys comma-separated and **aligned by index** — the *n*-th key belongs to the *n*-th endpoint. A
single key or model is reused for every endpoint; that's only correct when the resources genuinely
share it, so list one key per endpoint when they differ.

Clips are balanced **at the job level**: [`sora_config.next_resource()`](aismm/tools/sora_config.py)
round-robins a resource, and the whole create → poll → download lifecycle runs against that one.
A Sora job id only exists on the resource that created it, so this affinity is mandatory — putting a
round-robin gateway in front of the pool would send each call to a backend that never heard of the
job. If an attempt fails, `create_clip_with_failover` retries on a **different** resource (excluding
ones that already failed this clip), so a single endpoint that is out of credits, throttled, or
missing the deployment can't sink the post. The Azure response body is kept in the log line, which is
where the actual reason (`InsufficientQuota`, content filter, …) appears. With one resource
configured, the same code simply retries in place.

> **Note:** Sora 2's Videos API is announced for shutdown ~**Sep 24 2026**. The video tool sits behind
> the [tool registry](#add-a-tool) so a successor model can replace it in one file.

### Environment variable reference

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `azure` or `apim`. |
| `AZURE_OPENAI_MODEL` | Chat deployment name (both providers). |
| `AZURE_OPENAI_API_KEY` / `_ENDPOINT` / `_API_VERSION` | Azure-direct LLM. |
| `APIM_BASE_URL` / `APIM_SUBSCRIPTION_KEY` / `APIM_KEY_HEADER` / `APIM_API_VERSION` | APIM LLM. |
| `AZURE_OPENAI_ENDPOINT_SORA` / `_API_KEY_SORA` / `_MODEL_SORA` / `_API_VERSION_SORA` | Sora 2 pool (comma-separated, index-aligned). |
| `SORA_MAX_ATTEMPTS` | Resources one clip may try before failing; `0` = auto (pool size, capped at 3). |
| `AZURE_OPENAI_API_KEY_DALLE` / `_ENDPOINT_DALLE` / `_MODEL_DALLE` | Image generation. |
| `AISMM_TOKEN_KEY` | Fernet key for encrypting OAuth tokens (auto-generated to `tokens.key` if unset). |
| `AISMM_DATA_DIR` | Where the SQLite DB + generated assets live (default `./data`). |
| `STORE_BACKEND` | `auto` / `local` / `azure` — see [Storage](#storage-local-sqlite-or-azure-table--blob). |
| `AZURE_STORAGE_CONNECTION_STRING` | Storage account for Table + Blob (SandBox's `connection_string` also accepted). |
| `AISMM_TABLE_NAME` / `AISMM_BLOB_NAME` | Table and blob container names (default `aismm` / `aismm-media`). |
| `MEMORY_MAX_CHARS` | Size at which an instruction's [carry-over memory](#continuity-memory-notes-and-browsing) is summarized (default 6000). |
| `AI_DISCLOSURE_ENABLED` / `_TEXT` / `_SEPARATOR` | [AI-content disclosure](#ai-content-disclosure) — on by default. |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` / `DASHBOARD_BASE_URL` / `FLASK_SECRET_KEY` | Dashboard. |
| `AUTH_OIDC_ISSUER` / `_CLIENT_ID` / `_CLIENT_SECRET` / `_SCOPES` | [Dashboard SSO](#dashboard-sign-in-sso) — any OIDC provider. |
| `AUTH_ALLOWED_EMAILS` / `AUTH_ALLOWED_DOMAINS` | Who may sign in. Both empty = nobody. |
| `AUTH_PROVIDER_NAME` / `AUTH_SESSION_HOURS` / `AUTH_ENABLED` | Button label / session lifetime / force on-off. |
| `REVERSE_PROXY_PREFIX` | Optional dashboard path prefix, such as `/aismm`; applied to links, forms, assets, and OAuth URLs. |
| `INSTAGRAM_APP_ID` / `_APP_SECRET` | Meta app. |
| `TWITTER_CLIENT_ID` / `_CLIENT_SECRET` (+ `TWITTER_API_KEY`/`_API_SECRET`) | X app. |
| `GOOGLE_CLIENT_ID` / `_CLIENT_SECRET` | YouTube (Google) app. |
| `TIKTOK_CLIENT_KEY` / `_CLIENT_SECRET` | TikTok app. |
| `YOUTUBE_PRIVACY` / `TIKTOK_PRIVACY` | Default visibility (`private` / `SELF_ONLY`). |

Every account's OAuth **redirect/callback URL** is:
`<DASHBOARD_BASE_URL><REVERSE_PROXY_PREFIX>/oauth/<platform>/callback` (for example,
`https://your-host/aismm/oauth/twitter/callback` when the prefix is `/aismm`). Omit the prefix when
`REVERSE_PROXY_PREFIX` is empty. Register exactly this in each developer portal.

---

## Connecting accounts

For each platform you: (1) create a developer app, (2) put its credentials in `.env`, (3) register
the redirect URL above, then (4) click **Connect** on the dashboard's **Accounts** page and approve.

> Because OAuth requires a reachable redirect URL, connect accounts while the dashboard is running.
> For non-localhost redirects (required by most platforms in production, and by Instagram always),
> put a public HTTPS URL in `DASHBOARD_BASE_URL` — e.g. an [ngrok](https://ngrok.com) tunnel during
> development.

### Instagram

Uses the **Instagram Graph API** (content publishing). Requirements:

1. An **Instagram Business or Creator** account, **linked to a Facebook Page**.
2. A **Meta app** at <https://developers.facebook.com/apps> → add the **Instagram Graph API** product.
3. In the app, add the OAuth redirect `https://<your-host>/oauth/instagram/callback` (Facebook Login
   → *Valid OAuth Redirect URIs*).
4. Request the permissions `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
   `pages_read_engagement`, `business_management`. (App Review is needed to use these on accounts you
   don't own.)
5. Put `INSTAGRAM_APP_ID` / `INSTAGRAM_APP_SECRET` in `.env`.

Publishing is a two-step Graph call: **create a media container** (`POST /{ig-user-id}/media` — for
Reels `media_type=REELS` + a public `video_url`; for images `image_url`), **poll** its `status_code`
until `FINISHED` (videos/reels), then **`POST /{ig-user-id}/media_publish`**. AISMM resolves the IG
user id + Page token for you on connect. **Reels** must be 9:16, 5–90 s. ⚠️ Instagram **fetches media
from a public URL** — see the [caveat](#the-public-media-url-caveat).

### X (Twitter)

Uses **X API v2** with **OAuth 2.0 (PKCE)**.

1. Create a Project + App in the **X Developer Portal** (<https://developer.x.com>).
2. Enable **OAuth 2.0**, set the app type (a *confidential* client is simplest), and add the callback
   `https://<your-host>/oauth/twitter/callback`.
3. Scopes AISMM requests: `tweet.read tweet.write users.read media.write offline.access`
   (`offline.access` yields a refresh token; `media.write` enables media upload).
4. Put `TWITTER_CLIENT_ID` / `TWITTER_CLIENT_SECRET` in `.env`. (Optionally add
   `TWITTER_API_KEY`/`TWITTER_API_SECRET` — the OAuth 1.0a consumer keys — if you need the legacy
   v1.1 media-upload path; see the note in [`aismm/platforms/twitter.py`](aismm/platforms/twitter.py).)

Text posts use `POST /2/tweets`. Media (image/video) uses the chunked upload flow (INIT → APPEND →
FINALIZE → STATUS), then the `media_id` is attached to the tweet. **Text limit: 280 characters.**

### YouTube

Uses the **YouTube Data API v3** (`videos.insert`, resumable upload). **Video only.**

1. In **Google Cloud Console** (<https://console.cloud.google.com>), create a project and **enable the
   YouTube Data API v3**.
2. Configure the **OAuth consent screen** and create an **OAuth Client ID** (type: *Web application*).
3. Add the redirect `https://<your-host>/oauth/youtube/callback`.
4. Scope: `https://www.googleapis.com/auth/youtube.upload` (+ `youtube.readonly` to resolve the
   channel). AISMM sets `access_type=offline` + `prompt=consent` to obtain a refresh token.
5. Put `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `.env`.

The **first line** of the caption becomes the video **title** (≤100 chars); the rest is the
**description**. Default visibility is `private` (`YOUTUBE_PRIVACY`). Each upload costs ~**1,600**
quota units, so plan schedules accordingly.

### TikTok

Uses the **Content Posting API** (Direct Post). **Video only.**

1. Register an app on the **TikTok for Developers** portal (<https://developers.tiktok.com>) and add
   the **Content Posting API** product.
2. Add the redirect `https://<your-host>/oauth/tiktok/callback`.
3. Scopes: `user.info.basic`, `video.publish` (Direct Post), `video.upload`.
4. Put `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` in `.env`.

**Important:** Direct Post requires passing **TikTok's audit**. Until approved, every post is forced
to **`SELF_ONLY`** visibility (`TIKTOK_PRIVACY`), so only the creator can see it. AISMM sets the
**AI-generated content flag** on every TikTok post (AIGC labelling is required for AI content).

---

## Continuity: memory, notes, and browsing

A scheduled instruction that starts from scratch every run is useless for anything serial — "start
at 1 March and work through this site" would post the same 1 March item forever. Three pieces fix
that.

### Agent memory (written by the agent)

Each instruction carries a small working memory that persists **between runs**. The agent reads it
before choosing a topic and rewrites it before publishing, via two tools:

| Tool | What it does |
|---|---|
| `read_memory` | Returns the memory plus the operator note. |
| `update_memory(memory, append=False)` | Saves where it got to and what comes next. |

The memory is also **inlined into the next run's kickoff prompt**, not just left for the agent to
fetch — a scheduled run has to continue previous work, and that only happens reliably when the
previous position is in front of the model from the first turn. It looks like this:

```
CURRENT POSITION: covered news up to 2026-03-14.
NEXT STEP: continue with 2026-03-15 onward.
COVERED: 03-12 election piece, 03-13 budget piece, 03-14 weather.
LEARNED: the archive paginates 20 per page; ?page=N works.
```

**Summarization.** Past `MEMORY_MAX_CHARS` (default 6000) a small summarizer agent compresses it
after the run — instructed to preserve the position, next step, and learned facts *verbatim* and
compress only the history behind them, because losing the cursor defeats the whole feature. If the
summarizer fails, the memory is left exactly as it was. You can see and edit the memory (including
emptying it to start over) on the instruction's edit page.

### Operator note (written by you)

A free-text box on every instruction, for correcting a running agent **without touching the brief or
losing the memory**:

> *Search for more up-to-date content — the last few posts were stale.*

It is injected into every subsequent run and the agent is told to treat it as an override of its own
judgement. It applies from the next run; clear the box to withdraw it. The agent cannot edit it.

### Browsing real pages

`browse_page` opens a URL in headless Chromium (**Playwright** — the engine SandBox/AIBlog uses;
free, no API key) and returns the title, visible text, links, and media. `save_media` then downloads
one of those images or videos into the assets dir, so it can be passed to `publish` like generated
media.

Each image comes back with the context needed to pick the right one:

```json
{"url": "https://…/20260513_0335_da11.png", "alt": "Panel 1",
 "width": 1536, "height": 1024,
 "caption": "At dawn, Nerina drags Mira up the sealed lighthouse…"}
```

- `url` is the **full-resolution** source when the page exposes one in `data-full`, `data-src` or
  `srcset` — not the thumbnail or proxy URL in `src`.
- `alt` is often the identifier you need for ordered work ("Panel 1", "Panel 2").
- `caption` is the text of the block the image sits in — a comic panel's dialogue, a figure's caption.
- Favicons, tracking pixels and anything under 64px are filtered out.

**Pages that render from JavaScript** are the common failure. `browse_page` waits for the network to
go idle, forces `loading="lazy"` images to load, and scrolls — without that you get the loading
skeleton ("Generating…") and no images at all. If a page is still not ready, pass `wait_for` with a
CSS selector (`"img[alt^=Panel]"`) and it will wait for that element to appear.

Use it when the brief names a specific site; `web_search` remains better for open research.

```bash
pip install -r requirements.txt          # the playwright package
playwright install --with-deps chromium  # the browser binary (setup_service.sh does this)
```

Without the browser binary the two tools disable themselves and the agent works without them, the
same way the Sora tool does when unconfigured.

> **Two safety notes.** The agent chooses the URL, so private, loopback and link-local addresses are
> refused — on a cloud VM `169.254.169.254` would otherwise hand instance-metadata credentials to
> the model. And media you download belongs to someone else: the agent is told to post it only when
> the brief or your note says that source may be reused, and to credit it. Rights are your call, not
> the model's.

---

## When a run can't do its job, it fails

A run has **two** legitimate endings, and the agent picks one:

| Tool | Meaning |
|---|---|
| `publish` | There is a real post that satisfies the brief. |
| `report_failure` | The work could not be done — nothing is posted. |

`report_failure` records the run as **failed** with the agent's own diagnosis (what it tried, which
URLs, what came back), visible in the Runs table and the service log. A failed run is a normal
outcome; a wrong post is not.

Three things enforce that a blocked run doesn't turn into a bad post:

1. **The prompt** says publishing is not mandatory, and never to publish a post *about* the problem
   or a substitute for content it failed to fetch.
2. **The recovery nudge** (which fires when a run ends without a terminal call) offers both endings
   instead of demanding a publish.
3. **A code guard** refuses a caption written in the agent's own failure voice — "I was unable to
   retrieve…", "Unable to load…", tool names, "as an AI language model", apologies — and tells it to
   call `report_failure` instead. It is deliberately narrow so real copy survives: *"Investigators
   could not find the black box"* and *"I couldn't believe the sunrise"* both publish fine.
   `PUBLISH_CONTENT_GUARD=0` disables it.

A run that ends without calling either tool is also recorded as failed, rather than passing silently.

---

## AI-content disclosure

**Every post is labelled as AI-generated, automatically.** This is enforced in code next to the
publish-mode gate, not requested of the model — a disclosure the agent can forget is not a
disclosure.

Two layers, because a caption line is not what the platforms key on:

| Platform | Native API flag | What AISMM does |
|---|---|---|
| TikTok | `post_info.is_aigc` | sets it → *"Creator labeled as AI-generated"* |
| YouTube | `status.containsSyntheticMedia` | sets it → altered/synthetic disclosure |
| Instagram | none per-post (Meta infers from C2PA/IPTC metadata → its *"AI info"* label) | caption line |
| X | none per-post (its *"Made with AI"* toggle is UI-only) | caption line |

The caption suffix is appended within the platform's limit — the **caption** is trimmed to make
room, never the label:

```
Record rainfall hit the north coast today, per the weather serv

🤖 AI-generated
```

It applies on every path, so a `dry_run` preview and an `approval` queue item show exactly what
would be posted. A caption that already discloses ("made with AI", "#ai", …) is left alone rather
than double-labelled.

```ini
AI_DISCLOSURE_ENABLED=1
AI_DISCLOSURE_TEXT=🤖 AI-generated      # e.g. "Contenuto generato dall'IA"
AI_DISCLOSURE_SEPARATOR=\n\n
```

### Why it defaults to on

- **EU AI Act, Article 50** — transparency obligations apply from **2 August 2026**. Deployers must
  disclose AI-generated or manipulated image/audio/video that resembles real people, and
  AI-generated **text published to inform the public on matters of public interest**. Disclosure
  must be made *"at the latest at the time of the first interaction or exposure"*, clearly and
  distinguishably. Content published before 2 August 2026 does not need relabelling retroactively.
- **Platform rules** — Meta labels AI content as *"AI info"*, TikTok requires AIGC labelling,
  YouTube requires disclosing realistic altered/synthetic content, and X is rolling out
  *"Made with AI"* with mandatory disclosure already for some categories.

Two caveats worth understanding, because they may change what *you* need:

1. Article 50's text obligation **does not apply** where content had *"human review or editorial
   control and a natural or legal person holds editorial responsibility"* — and the review must be
   substantive, not a cursory approval. AISMM's `approval` publish mode is a plausible basis for
   that; `live` mode is not, since nobody sees the post before it goes out.
2. The machine-readable marking duty (watermarks, C2PA provenance) falls on the **provider** of the
   generative model — Azure OpenAI for Sora/gpt-image — not on you as the deployer. AISMM does not
   strip that metadata.

This is engineering, not legal advice. Whether these rules bind your posts depends on where you and
your audience are; confirm it for your situation.

---

## Storage: local SQLite, or Azure Table + Blob

Two interchangeable backends behind the same `Store` interface:

| Backend | State | Media |
|---|---|---|
| **local** (default) | SQLite at `AISMM_DATA_DIR/aismm.sqlite` | files under `AISMM_DATA_DIR/assets/`, served by the dashboard |
| **azure** | one Azure **Table** | an Azure **Blob** container |

```ini
STORE_BACKEND=auto            # auto | local | azure  (auto = azure once a connection string is set)
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...
AISMM_TABLE_NAME=aismm        # one table holds everything
AISMM_BLOB_NAME=aismm-media   # container for generated/downloaded media
```

Get the connection string from **Storage account → Security + networking → Access keys**. The table
and the container are **created on first use** — no manual provisioning. Already running SandBox?
Its shared lowercase `connection_string` variable is accepted verbatim, so an existing SandBox `.env`
works as-is.

### How it is laid out (the SandBox convention)

One table per project, entity type in the `PartitionKey`, id in the `RowKey` — the same shape
`GenBox/azurestorage.py` and `ComicBook/azurestorage.py` use:

| PartitionKey | RowKey | Contents |
|---|---|---|
| `account` | account id | connected profile + **encrypted** tokens |
| `instruction` | instruction id | brief, schedule, publish mode |
| `run` | run id | one execution |
| `staged` | staged id | preview / approval-queue item |
| `state` | instruction id | carry-over memory + operator note |
| `lock` | lock key | single-flight lock |

Locks use `create_entity` + `ResourceExistsError` with a TTL reclaim — `GenBox._try_acquire_lock`,
ported. Tokens are Fernet-encrypted **before** they reach the table, exactly as with SQLite, so the
storage account never holds a usable credential.

### Blob storage solves the Instagram problem

With blob storage configured, every generated or downloaded asset is uploaded to the container and
`public_url()` returns the **blob URL**. Instagram fetches media from a public URL rather than
accepting an upload, so this removes the need for the dashboard itself to be publicly reachable.

> **Set the container access level to "Blob (anonymous read access for blobs only)"** — otherwise
> uploads still succeed but Instagram gets a 403 fetching the media.

A local copy is always written as well: X, YouTube and TikTok upload the bytes directly, and reads
fall back to downloading from the blob when the local file is missing — so a second host, or a wiped
data dir, doesn't break publishing.

Two limits worth knowing: Table Storage caps a single property at 64 KB (AISMM raises a clear error
past 32 000 characters rather than letting Azure return an opaque 400), and it cannot sort or
paginate server-side, so listings are sorted in Python — the same thing `GenBox.get_last_n_rows`
does. Neither matters at this scale.

There is **no migration** between backends: switching points AISMM at empty storage. Connect
accounts and recreate instructions there, or copy the rows yourself.

---

## The public-media-URL caveat

**Instagram fetches your media from a public URL** — it does not accept a direct binary upload. AISMM
serves generated assets at `<DASHBOARD_BASE_URL><REVERSE_PROXY_PREFIX>/assets/<file>`, so
**`DASHBOARD_BASE_URL` must be a public HTTPS address** for Instagram to work (use an ngrok tunnel
locally, or deploy the dashboard). If it points at `localhost`/`127.0.0.1`, the Instagram
integration raises a clear error instead of failing silently.

X, YouTube, and TikTok upload the bytes directly, so they work without a public URL. For a fully
cloud-hosted setup, implement the Azure Blob adapter (`aismm/store/azure_store.py`) and serve media
from Blob with a public/SAS URL.

### Images are converted locally to what the platform accepts

Instagram takes **JPEG only** (8 MB max, aspect ratio 4:5–1.91:1, width ≤1440) and rejects anything
else with `Media download has failed` — an error that points at the URL when the problem is the
*file*. So before publishing, [`media.py`](aismm/media.py) normalizes the image with Pillow:

| Problem | Fix |
|---|---|
| WebP scraped from a page, PNG from `generate_image` | re-encoded to JPEG |
| Transparency (RGBA/palette) | flattened onto a background — JPEG has no alpha |
| 1024×1536 portrait (ratio 0.67, below Instagram's 0.8 floor) | **padded** to the nearest allowed ratio |
| Wider than 1440px, or over 8 MB | downscaled; JPEG quality steps down until it fits |

Padding rather than cropping is deliberate: cropping an AI-generated image can cut the subject out.
The pad colour is sampled from the image, so the bars are unobtrusive.

Conversion runs inside the publish gate, so the dry-run preview, the approval queue and the live post
all reference the same converted file — and with blob storage on, the converted JPEG is the one
uploaded and fetched. Platforms that declare no image constraints (X) get their asset untouched, and
a file Pillow can't read is passed through unchanged so the platform's own error surfaces.

Video is **not** re-encoded — that needs ffmpeg, which is not a dependency. Sora outputs MP4, which
every target accepts; a WebM saved from a web page will be rejected by the platform.

### When an Instagram publish fails

Failures surface Graph's own error body — message, `code`, `error_subcode`, `error_user_msg` and
`fbtrace_id` — because `400 Bad Request` on its own says nothing:

```
Instagram Graph 400: The media is not eligible [code=352 · error_subcode=2207026 · fbtrace_id=…]
```

Common ones: **code 9007** ("Media ID is not available") means the container wasn't ready — AISMM
waits for `status_code=FINISHED` and retries the publish, so this should self-resolve; **code 352 /
subcode 2207xxx** is a Reels format problem (9:16, 5–90s, H.264/AAC); **code 190** is an expired
token — reconnect the account. A 25-posts-per-24h cap also applies per account.

The access token is sent as an `Authorization: Bearer` header, never as a URL parameter, so it can't
end up in exception messages or the service log.

---

## Running it

```bash
python -m aismm.cli run          # scheduler + dashboard (default)
python -m aismm.cli dashboard    # dashboard only (no scheduler)
python -m aismm.cli scheduler    # scheduler only (headless)
python -m aismm.cli list         # list connected accounts + instructions
python -m aismm.cli auth twitter # print a platform connect URL
python -m aismm.cli post --instruction <id-or-name> [--account <id>]   # run once, now
```

(If you `pip install -e .`, the same commands are available as the `aismm` console script.)

**Schedules** accept a 5-field cron expression (`0 9 * * *`) or an interval (`every 6h`, `30m`,
`1d`). Editing an instruction in the dashboard live-reschedules its job.

### Logs

```bash
journalctl -u aismm.service -f          # on the server
LOG_LEVEL=DEBUG python -m aismm.cli run # locally, with httpx request lines
```

`LOG_LEVEL` (default `INFO`) is applied by [`logging_setup.py`](aismm/logging_setup.py), called from
every entrypoint. At `INFO` a run narrates itself end to end:

```
RUN START a1b2c3d4 | instruction='Daily news' account=genaicomicbook (instagram) mode=live media_pref=auto
Agent ready: 9 tool(s) [get_context, read_memory, …], memory=412 chars, note=yes
Sora clip failed (attempt 1/3 on pocs-openai-gioak…): HTTP 401 … InsufficientQuota
Padded image 1024x1536 -> 1242x1536 to reach an allowed aspect ratio
Publish requested: mode=live platform=instagram kind=image caption=137 chars asset=…jpg
Instagram media: 3f1df1be.jpg kind=image bytes=11,526 format=JPEG mode=RGB size=1242x1536 ratio=0.809
Instagram media URL check: HTTP 200 content-type=image/jpeg length=11526 url=https://…/3f1df1be.jpg
Instagram container 179000… created (image, caption 137 chars)
Instagram container 179000…: FINISHED (6s elapsed)
LIVE published to instagram: https://instagram.com/p/…
RUN DONE  a1b2c3d4 | 84.3s | {'mode': 'live', 'url': …}
```

Third-party loggers (httpx, the Azure SDKs, APScheduler) are pinned quieter so they don't bury the
run — `LOG_LEVEL=DEBUG` unpins them.

> Before this existed, nothing below WARNING was ever emitted: Python's root logger defaults to
> WARNING and nothing configured it, so every `logger.info(...)` in the codebase was discarded. If
> you see only tracebacks in `journalctl`, you are running a build from before this change.

### Debugging in VS Code

The repo ships a shared [`.vscode/`](.vscode) config (launch profiles, tasks, pytest wiring). Pick a
profile from the Run and Debug panel:

| Profile | What it starts |
|---|---|
| **AISMM: Run (scheduler + dashboard)** | Both in one process — the normal way to debug (`aismm run`) |
| **AISMM: Full Stack (dashboard + scheduler)** | Compound: the two as **separate** debuggable processes |
| AISMM: Dashboard (frontend) | Dashboard only, no scheduler |
| AISMM: Scheduler (backend) | Headless scheduler only |
| AISMM: Post one instruction | Prompts for an instruction id/name and runs it once |
| AISMM: Smoke test LLM / Sora | The `scripts/smoke_*.py` checks |
| AISMM: Attach (debugpy :5678) | Attaches to an already-running process |

Breakpoints work inside the agent and the SDK (`justMyCode: false`), and the Flask reloader is off so
the debugger keeps its process. Before the first launch, run the **aismm: setup venv + install deps**
task (`Terminal → Run Task`) and copy `.env.example` to `.env`.

Use the compound profile when you want to step through a scheduled run and a dashboard request
independently. One caveat in that split mode: the dashboard's live re-scheduling talks to its own
in-process scheduler, so instruction edits reach the separate scheduler process only when it
restarts. The single-process profile has no such gap.

### Deploying on a server (systemd)

[`setup_service.sh`](setup_service.sh) installs — or updates and restarts — a systemd unit that runs
the dashboard and scheduler under Gunicorn. Re-run it after every `git pull`; it is idempotent.

```bash
sudo ./setup_service.sh
```

Re-running is cheap: pip runs **only when `requirements.txt` has changed** (a hash is stamped into
the venv), and Chromium is downloaded only when it's missing. `FORCE_INSTALL=1` reinstalls anyway.

It creates `.venv` and installs the deps if missing, creates `.env` from the example on first run
(then stops so you can fill it in), fixes ownership on `data/` and `tokens.key`, writes
`/etc/systemd/system/aismm.service`, and enables + (re)starts it. Overrides:

```bash
sudo SERVICE_NAME=aismm SERVICE_USER=ubuntu BIND_ADDR=0.0.0.0:8787 THREADS=8 ./setup_service.sh
sudo SKIP_INSTALL=1 ./setup_service.sh    # leave the venv/pip alone
```

```bash
journalctl -u aismm.service -f
```

The unit serves [`aismm/wsgi.py`](aismm/wsgi.py) with **one worker and multiple threads** — the
scheduler has to share a process with the dashboard for live re-scheduling to work, so scale with
`THREADS`, not workers. Set `AISMM_ENABLE_SCHEDULER=0` in `.env` to serve the dashboard alone.
`--timeout 1800` is deliberate: an agent run plus a video upload can take many minutes.

Put a TLS reverse proxy (nginx/Caddy) in front of it and set `DASHBOARD_BASE_URL` to that public
https URL — OAuth callbacks and Instagram's media fetch both depend on it. Anything reachable from
the internet must also turn on [dashboard sign-in](#dashboard-sign-in-sso); without it the dashboard
is open to whoever finds the URL.

To expose AISMM below a path, set `REVERSE_PROXY_PREFIX` as well. For example:

```dotenv
DASHBOARD_BASE_URL=https://example.com
REVERSE_PROXY_PREFIX=/aismm
```

Then proxy `/aismm/` to `http://127.0.0.1:8787/`. AISMM accepts the prefix whether the proxy strips
it or passes it upstream, and automatically generates paths such as `/aismm/runs`,
`/aismm/assets/...`, and `/aismm/oauth/<platform>/callback`.

---

## Extending the framework

### Add a platform

Subclass `SocialPlatform`, declare its OAuth endpoints/scopes + `Capabilities`, implement
`fetch_identity` and `publish`, then register it:

```python
# aismm/platforms/linkedin.py
from .base import SocialPlatform, Capabilities
from .registry import register
from ..models import PlatformName   # add the enum member too

class LinkedIn(SocialPlatform):
    name = PlatformName.linkedin
    capabilities = Capabilities(supports_text=True, supports_image=True, supports_video=True,
                                needs_public_media_url=False, default_orientation="landscape",
                                caption_limit=3000)
    auth_endpoint = "..."; token_endpoint = "..."; scopes = [...]
    async def fetch_identity(self, access_token): ...
    async def publish(self, *, access_token, account, caption, asset_path, media_kind): ...

register(PlatformName.linkedin, LinkedIn)
```

### Add a tool

A tool factory is `fn(state) -> Tool | None`. Register it and the manager agent gets it on every run:

```python
# aismm/tools/music_tool.py
from agents import function_tool
from .registry import register_tool

def _make_add_music(state):
    @function_tool
    async def add_music(asset_path: str, mood: str) -> dict:
        """Add a licensed music bed to a video."""
        ...
    return add_music

register_tool("add_music", _make_add_music)
```

Import it in `aismm/tools/__init__.py` so the registration runs. See
[`aismm/tools/registry.py`](aismm/tools/registry.py).

---

## Project layout

```
aismm/
├── config.py            # env → typed Settings (LLM toggle, Sora pool, paths, platform creds)
├── llm.py               # Azure-direct OR APIM client + OpenAIResponsesModel
├── crypto.py            # Fernet token encryption
├── models.py            # SQLModel: Account, Instruction, Run, StagedPost + enums
├── assets.py            # generated-media storage + public URLs
├── orchestrator.py      # run_instruction() per-account + lock + approval publishing
├── scheduler.py         # APScheduler daemon (cron/interval parsing)
├── cli.py               # `aismm run|dashboard|scheduler|auth|list|post`
├── agent/               # manager_agent.py (Runner.run + recovery) + prompts.py
├── tools/               # registry + web_search, sora_client, video/image/publish/context tools
├── platforms/           # base + registry + instagram / twitter / youtube / tiktok
├── store/               # base + local_store (SQLite) + azure_store (adapter stub)
├── auth/                # generic OAuth2 (+ PKCE) helper
└── dashboard/           # Flask app + templates + static (the control center)
```

## Dashboard sign-in (SSO)

The dashboard controls connected social accounts and can publish on your behalf, so **any deployment
reachable from the internet must be behind a login**. AISMM has no user database and no passwords:
it delegates *who you are* to an **OpenID Connect** provider and decides *whether you may in* from an
allowlist you control.

Any OIDC provider works — **Google**, **Microsoft Entra ID**, Okta, Auth0, Keycloak — because every
endpoint is read from the issuer's discovery document. Only three values differ between them:

```dotenv
AUTH_OIDC_ISSUER=https://accounts.google.com
AUTH_OIDC_CLIENT_ID=<client id>
AUTH_OIDC_CLIENT_SECRET=<client secret>

AUTH_ALLOWED_EMAILS=you@example.com     # who may actually sign in
AUTH_PROVIDER_NAME=Google               # button label only
```

The guard switches on as soon as those three `AUTH_OIDC_*` values are set. Signed-out visitors get a
sign-in page; everything else 302s there until a session exists.

### Register the redirect URI

Whatever the provider, register exactly this callback (prefix included):

```
<DASHBOARD_BASE_URL><REVERSE_PROXY_PREFIX>/auth/callback
```

For a deployment at `https://example.com/aismm/`, that is `https://example.com/aismm/auth/callback`.

### Issuer values

| Provider | `AUTH_OIDC_ISSUER` | Where the client id/secret come from |
|---|---|---|
| Google | `https://accounts.google.com` | Cloud Console → APIs & Services → Credentials → **OAuth client ID** (type *Web application*) |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant-id>/v2.0` | Entra admin center → App registrations → your app → **Certificates & secrets** |
| Okta | `https://<org>.okta.com` | Okta admin → Applications → *OIDC Web Application* |

**Entra ID works and is a good fit if you already use Azure** — it's the same code path, only the
issuer differs. Two provider-specific notes AISMM already handles: Entra often sends
`preferred_username`/`upn` instead of `email` (all three are accepted, with the userinfo endpoint as
a fallback), and its multi-tenant discovery advertises a templated `{tenantid}` issuer that is
resolved from the token's `tid` claim. Using your **tenant-specific** issuer URL (with the real
tenant id, not `common`) is the stricter choice — it means only your own directory can issue tokens
this app accepts.

### Who gets in

Authenticating is **not** enough — the identity must match the allowlist:

```dotenv
AUTH_ALLOWED_EMAILS=you@example.com,teammate@example.com
AUTH_ALLOWED_DOMAINS=yourcompany.com          # exact domain match, no subdomains
```

> **Both empty means every login is refused.** That is deliberate. With a public issuer like Google,
> "authenticated" means *any Google account on earth*, so an empty allowlist can't be treated as
> "allow all". Set at least one entry.

### What is and isn't protected

| Path | Protected | Why |
|---|---|---|
| `/`, `/accounts`, `/instructions`, `/runs`, `/settings`, all POSTs | ✅ | The whole control surface |
| `/oauth/<platform>/*` | ✅ | Connecting social accounts |
| `/assets/<file>` | ❌ **public by design** | Instagram fetches media from this URL server-side, with no cookie — guarding it breaks publishing. Filenames are `uuid4`, so the URL is the secret. |
| `/healthz` | ❌ | Liveness probe for the proxy |
| `/login`, `/auth/callback`, `/logout` | ❌ | The sign-in flow itself |

Other details worth knowing:

- The session is a signed cookie — **set a strong `FLASK_SECRET_KEY`**, or a forged cookie is a valid
  login. `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `Secure` is set automatically when `DASHBOARD_BASE_URL` is https; `HttpOnly` and `SameSite=Lax`
  always. Sessions last `AUTH_SESSION_HOURS` (default 12).
- ID token signatures are **not** verified, which is safe here specifically: the token is fetched by
  the server directly from the provider's token endpoint over TLS, never accepted from the browser
  ([OIDC Core §3.1.3.7](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation)).
  Issuer, audience, expiry and nonce are all checked. See [`sso.py`](aismm/dashboard/sso.py).
- `AUTH_ENABLED=0` force-disables the guard for local development. It logs a loud warning.

Implementation: [`aismm/dashboard/sso.py`](aismm/dashboard/sso.py), tests in
[`tests/test_sso.py`](tests/test_sso.py).

---

## Security

- OAuth tokens are **encrypted at rest** (Fernet). Keep `tokens.key` and `.env` out of version
  control (both are git-ignored). Set `AISMM_TOKEN_KEY` to pin the key across machines.
- Never commit real credentials. **Put the dashboard behind [SSO](#dashboard-sign-in-sso)** whenever
  it is reachable from anything but localhost, and terminate TLS in front of it.
- Start every new instruction in `dry_run`, review the previews, then move to `approval` or `live`.

## Caveats & limitations

- **Live posting requires real, approved developer apps.** Instagram needs a Business account + App
  Review; TikTok needs its audit (else `SELF_ONLY`); YouTube uploads consume quota.
- The hosted `WebSearchTool` requires your Azure deployment/region to expose it. If it isn't
  available, swap `aismm/tools/web_search.py` for a function-tool fallback (LangChain
  `{"type":"web_search"}`, Tavily, or DuckDuckGo) — one file.
- TikTok single-chunk upload is used (fine for short clips); very large files would need multi-chunk.
- Token auto-refresh isn't run proactively; reconnect an account if its token expires. (Refresh
  helpers exist on each platform for you to wire into a pre-publish check.)

## References

- Instagram — [Content Publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/)
- X / Twitter — [Create Post](https://docs.x.com/x-api/posts/create-post)
- YouTube — [Resumable uploads](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol) · [Upload a video](https://developers.google.com/youtube/v3/guides/uploading_a_video)
- TikTok — [Developers](https://developers.tiktok.com/) (Content Posting API)
- Sora 2 video — [OpenAI](https://developers.openai.com/api/docs/guides/video-generation) · [Azure](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/video-generation)
- OpenAI Agents SDK + Azure/APIM — [Microsoft Community Hub](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/use-azure-openai-and-apim-with-the-openai-agents-sdk/4392537)
