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
- [Workspaces (several people, one deployment)](#workspaces)
- [Platform apps (credentials in the UI)](#platform-apps-credentials-in-the-dashboard)
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

### Videos longer than 12 seconds

Sora 2 renders 4, 8 or 12 second clips, so **the agent decides the length and the system builds it
from several clips**:

| Tool | What it does |
|---|---|
| `plan_video(target_seconds)` | turns "a one-minute reel" into a segment plan — `60s → 5 × 12s` — and reports the total actually achievable |
| `create_video_sequence(scenes, style, …)` | renders one shot per scene *with continuity*, then merges them into one MP4 |

`30s` is not reachable with 4/8/12 clips, so the plan says so rather than quietly returning 32s.
Merging uses **imageio-ffmpeg**'s bundled binary — `pip install`, no system ffmpeg (same as GenBox).

#### Keeping the shots looking like one video

This is where GenBox drifts even when a reference is passed. Three causes, each addressed:

1. **The style text now goes into *every* shot's prompt**, verbatim. GenBox put its "bible" only in
   the first clip of a speaker and trusted the reference to carry the look afterwards. Repetition is
   the cheapest and most reliable lever Sora gives you — so `style` is a first-class argument and the
   agent is told to keep it identical across the run.
2. **The reference frame is now *described* as a continuation.** Sora treats `input_reference` as a
   loose starting point, so a prompt that just describes the next action invites a new scene. Shot 2
   receives:

   ```
   STYLE (keep identical in every shot): A calm 40-year-old presenter in a navy suit on a harbour wall at dawn, overcast light, 35mm lens…
   CONTINUITY: the supplied reference image is the FINAL FRAME of the previous shot. Begin this shot
   from that exact framing, lighting, subject and wardrobe, then perform the action below. Do not
   restyle, recolour, relight or reframe the scene, and do not cut to a new location.
   SHOT 2 of 5: she lifts the kite into the wind
   ```
3. **The whole sequence is pinned to one Sora resource.** A remix only exists on the resource that
   made its base clip, so GenBox's per-clip failover silently destroyed the option. Here only shot 1
   picks a resource; every later shot stays on it.

`continuity="auto"` (default) chains frames and **falls back to remixing the previous shot** if the
reference is refused — Azure rejects `input_reference` containing human faces, which is exactly when
GenBox loses continuity. `continuity="remix"` derives each shot from the one before it (strongest
when people are on camera); `"frame"` chains only; `"none"` makes independent clips.

> **Remix chains from the previous shot, not from shot 1.** Anchoring every remix to shot 1 was the
> original design, and it published a reel whose opening moment played three times over: each later
> shot applied its own prompt to the same untouched starting point, so the action never advanced.
> Because a refused reference is the *normal* case as soon as people are on camera, that path ran
> for nearly every shot. Chained drift is the lesser evil — repetition is not a video.

> **A remix cannot change the clip's length.** The remix API takes only a prompt and inherits the
> source's duration, so a shot asking for 8s that falls back to remixing a 4s clip renders 4s — a
> 4/4/4/8 plan came out as 16s, not 20s. Every clip is therefore measured after rendering:
> `shots[].seconds` is the real length, `requested_seconds` appears only when it differs, and the
> result carries a `warning` telling the agent to describe the real duration. Use `continuity="none"`
> when the exact length matters more than the visual match.

> **Sora 2 has no seed.** None of this makes shots identical — it makes them plausibly the same
> scene. That ceiling is the model's. Keep `style` rich, and make each scene the *next step* in the
> action rather than a restatement of the last.

A shot that fails does not lose the ones already rendered: the video is merged from what succeeded
and the result carries a `warning` naming the shortfall.

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

## Workspaces

Several people can share one deployment without sharing everything. A **workspace** is a silo: its
own connected social accounts, instructions, runs and approval queue. Nothing crosses between them —
an account connected in one workspace cannot be posted to from another, which is what makes a
private workspace actually private. Partitioning only the instructions would have left every member
able to publish to every connected account.

Switch workspace from the selector in the header; everything else on the page follows it.

**There is one kind of workspace.** Every one is **private to whoever created it until they add a
member**, and every one can be shared — including the workspace you started with. Nothing is
permanently locked to one person, and nothing is permanently open to everyone.

**Signing in for the first time gets you your own workspace and nothing else** — `Sam's workspace`,
empty, owned by Sam. A new colleague writes their own instructions rather than opening the dashboard
onto someone else's live accounts. They see another workspace only when its owner invites them, and
even then they still *land* in their own.

**Sharing.** On **Workspaces**, an owner adds someone by email and picks their role:

| | |
|---|---|
| **Owner** | Manage membership, connect and disconnect accounts, rename or delete the workspace. |
| **Member** | The everyday work: author instructions, run them, approve posts. |

**Membership is not sign-in.** They are separate gates and stay that way: adding someone to a
workspace does not let them log in. They also need to pass the SSO allowlist
(`AUTH_ALLOWED_EMAILS` / `AUTH_ALLOWED_DOMAINS`), and the dashboard tells you when the person you
just added does not.

**Deleting.** A workspace can only be deleted once it is empty. Its content is never cascaded away
for you: instructions and runs cannot be recovered, and its accounts still hold live access tokens.
The last owner cannot be removed either — a workspace with no owner could never have its membership
changed again.

**Without SSO** the dashboard is unauthenticated anyway, so inventing a user there would guard
nothing: one implicit local operator owns every workspace and sees everything. Local development is
unchanged, and you can still create and switch workspaces to try the feature.

**Upgrading an existing deployment.** Content written before workspaces existed carries no
workspace, and exactly one workspace owns those rows when it reads them — no migration step to run,
and nothing that can be lost by a migration that was interrupted. The first person to sign in after
the upgrade inherits that workspace, which is renamed to theirs: you land on everything you already
had, in `Your name's workspace`, private to you. Colleagues who sign in later start empty.

`python -m aismm.cli workspaces` prints every workspace, whether it is private or shared, its members
and what it holds. Two flags:

```bash
# take ownership of a workspace (adds you if you are not a member yet)
python -m aismm.cli workspaces --owner you@example.com --rename "Your workspace"

# optional tidy-up: write the owning workspace onto rows that predate workspaces
python -m aismm.cli workspaces --adopt
```

`--owner` defaults to the workspace holding pre-existing content; pass
`--workspace <id or name>` for any other. It only ever promotes — an existing owner stays one. It is
the direct route to the repair a sign-in performs anyway, and the way out if a workspace ever ends up
with no owner (with none, its membership can never be changed by anyone).

**What is NOT scoped:** platform *app* credentials (Apps). Those are deployment infrastructure — one
Meta app, one X app — and the sensitive part, the access token, lives on the account, which is
scoped.

---

## Platform apps: credentials in the dashboard

**Apps → pick a platform** holds the OAuth credentials AISMM connects through, so you no longer need
to edit `.env` and redeploy to change them — and you can register **several apps per platform**, one
per brand or client. Each connected account records which app authorised it.

The page shows the setup steps beside the form: where the credentials live in that platform's
console, links to the console and its docs, the exact redirect URI to register (with your
reverse-proxy prefix already applied), and the platform-specific traps — for Instagram, that the
*Instagram app ID* is **not** the value the login dialog wants.

Secrets are Fernet-encrypted at rest, exactly like account tokens, and are never rendered back into
the form; leaving the secret box empty when editing keeps the stored one.

**`.env` and dashboard apps work side by side — both are always offered.** The environment
credentials are the *default* (an account connected before the Apps page existed still resolves to
them, so reconnecting one never silently switches it to a different app), and every dashboard app is
listed alongside. The Apps page shows the `.env` credentials as their own card with a Connect button.

### Connecting more than one account per platform

The Accounts page shows one **Connect** button per credential source — `from .env (default)` plus
each configured app — so you can keep an existing `.env`-connected account and add more through the
UI:

```
Connect · from .env (default) →
Connect · Brand B — Meta app →
```

> One caveat for Instagram specifically: `fetch_identity` picks the **first** Facebook Page that has
> an Instagram business account attached. If one login administers several such Pages, only the first
> is reachable today — connect the second through its own Meta app / Facebook login, or ask for a
> Page picker to be added.

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

`save_media` identifies files by their **bytes**, not the `Content-Type` header. Storage written
without a content type serves `application/octet-stream` (Azure Blob does this routinely), and a
perfectly good PNG then looks like a binary blob — trusting the header meant refusing real media. The
order is magic numbers → Pillow → declared content type → URL extension, and a server that positively
declares something non-media (an HTML error page at a `.jpg` URL) is still refused.

The saved file goes into your own storage — local assets dir, and the blob container when Azure is
configured — and is then [converted to the target platform's format](#images-are-converted-locally-to-what-the-platform-accepts)
at publish time. A 1536×1024 PNG panel becomes a 1440×960 JPEG for Instagram automatically.

### Seeing an image

Everything else the agent receives is text. `browse_page` hands it a URL, alt text and the
surrounding caption — never the picture — so a page whose meaning lives in the image left it guessing
from filenames.

`describe_image` closes that gap. Give it an `asset_path` (from `save_media` or `generate_image`) or a
public image URL, plus an optional question:

```
describe_image("/…/assets/8d8cfa00.png", "which character is holding the letter?")
```

A small vision agent — separate from the manager, with no tools and no ability to publish — looks at
the image and answers. It is told to transcribe any text it sees exactly, which is usually the useful
part: speech bubbles, chart labels, signage, UI text.

Reach for it when the answer is *in* the picture: reading a comic panel, telling several similar
images apart, putting frames in order, or checking that a generated image came out as asked. It costs
a model call, so the prompt tells the agent to use it when the surrounding text is not enough rather
than on every image it meets. Images only — a video cannot be described this way.

The same SSRF guard as browsing applies (the agent picks the URL, so private and loopback addresses
are refused), the bytes are sniffed rather than trusted, so an HTML error page served at a `.png` URL
never becomes a hallucinated description, and anything oversized is downscaled before it is sent. If
the call fails — a deployment without vision support, for instance — the tool says so and tells the
agent it may carry on without having seen the image, rather than ending the run.

**Pages that render from JavaScript** are the common failure. `browse_page` waits for the network to
go idle, forces `loading="lazy"` images to load, and scrolls — without that you get the loading
skeleton ("Generating…") and no images at all. If a page is still not ready, pass `wait_for` with a
CSS selector (`"img[alt^=Panel]"`) and it will wait for that element to appear.

**Content behind a button is a different problem, and waiting never fixes it.** A modal, tab or
accordion may not have its content in the page *at all* until the control is pressed — an `<img>` can
sit there with no `src` whatsoever. So the result also lists the page's clickable `buttons` with
ready-to-use selectors, and `click` presses one before reading:

```python
browse_page(url, click="#charSheetLink")   # opens the modal, then reads the page
```

Buttons are not links, so they never appear under `links` — without the `buttons` list the agent
cannot even discover that such a control exists. If the selector matches nothing the page is still
read and the result carries `click_failed` telling the agent to check `buttons`.

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
AI_DISCLOSURE_ENABLED=1                 # global master switch
AI_DISCLOSURE_TEXT=🤖 AI-generated      # e.g. "Contenuto generato dall'IA"
AI_DISCLOSURE_SEPARATOR=\n\n
```

**Per instruction**, a checkbox — *"Add the AI-generated label"* — turns it off for that instruction
alone. The global switch is the master: an instruction can opt *out* below it, never back on.

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
ported — and are **heartbeated** while their run is alive (see below). Tokens are Fernet-encrypted
**before** they reach the table, exactly as with SQLite, so the storage account never holds a usable
credential.

### Why a run can't block the next one forever

Each `(instruction, account)` pair is single-flighted by a lock, so a double schedule fire never
double-posts. The subtlety is what happens when the lock's owner *dies*.

The dashboard's **Run now** executes in a plain daemon thread, not a scheduler job. If the service
restarts while it is in flight (`Restart=always` will), that thread is killed without unwinding its
`finally` — the lock is left behind, and every scheduled run of that instruction is refused as
"already running" by a run that no longer exists. That is why a manual run could stop scheduled ones
*after* it had finished.

So a live run **renews its lock every 60 seconds** and the lock's TTL is 300. A run may take as long
as it likes; an abandoned lock is reclaimable 5 minutes after whatever held it stopped breathing.

Separately, every run has a **wall-clock ceiling of one hour**. APScheduler executes jobs in a bounded
thread pool with `max_instances=1`, so a run that never returns would silence its instruction
permanently and leak a pool thread. A skipped fire is now logged rather than silent:

```
Instruction job instr:a795… did NOT fire: its previous run is still going.
```

A `RUN START` with no matching `RUN DONE` is the signature of a wedged run.

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
| Wider than 1440px, or over 8 MB | downscaled with LANCZOS; JPEG quality steps down only if still too big |
| Already a compliant JPEG | left **exactly** as it is — no re-encode, no generation loss |

Padding rather than cropping is deliberate: cropping an AI-generated image can cut the subject out.
The pad colour is sampled from the image, so the bars are unobtrusive. Ratio is fixed *before* the
width clamp, so a padded image can never come out wider than the platform's limit.

**Quality is kept as high as the platform allows.** The first JPEG pass is **q95 with full 4:4:4
chroma**, downscaling uses **LANCZOS**, and quality only steps down if the byte cap actually bites —
a 1440px panel uses a few percent of Instagram's 8 MB, so there is no reason to spend that budget on
compression. Measured on a line-art panel:

| | PSNR vs source |
|---|---|
| LANCZOS downscale only, no JPEG — the ceiling | 26.60 dB |
| **What AISMM publishes now** | **26.57 dB** |
| Previous settings (q88, 4:2:0, bicubic) | 24.52 dB |

In other words the encode is now essentially free; everything left is the unavoidable 1536→1440
resize. And an image that **already** meets the platform's limits is passed through byte-for-byte
rather than re-encoded, so publishing the same asset twice doesn't degrade it twice.

Conversion runs inside the publish gate, so the dry-run preview, the approval queue and the live post
all reference the same converted file — and with blob storage on, the converted JPEG is the one
uploaded and fetched. Platforms that declare no image constraints (X) get their asset untouched, and
a file Pillow can't read is passed through unchanged so the platform's own error surfaces.

Video is **not** re-encoded — that needs ffmpeg, which is not a dependency. Sora outputs MP4, which
every target accepts; a WebM saved from a web page will be rejected by the platform.

### What the agent can do on Instagram

Beyond a single feed post, the Instagram integration covers the rest of the account. The engagement
tools appear **only on runs that target an Instagram account** — a TikTok run gets 10 tools, an
Instagram run 18 — so other platforms aren't handed irrelevant ones.

**Publishing**, all through the one gated `publish` tool:

| Post | How |
|---|---|
| Feed image / video (Reel) | as before |
| **Carousel** (2–10 items) | `asset_paths=[…]` — child containers, then a `CAROUSEL` parent |
| **Story** | `placement="story"` |

Mixed carousels work (images + video); a video *inside* a carousel is `media_type=VIDEO`, not `REELS`
— that's Meta's rule and getting it wrong is a rejected container. Stories carry **no caption**, so
words have to be in the image; the agent is told this.

**Reading and engagement:**

| Tool | What it's for |
|---|---|
| `instagram_recent_posts` | the existing feed with captions + like/comment counts — stops the agent repeating itself and lets it match its own voice |
| `instagram_comments` | comments on a post, with replies |
| `instagram_reply_to_comment` | answer publicly, in the account's voice |
| `instagram_moderate_comment` | hide (preferred), unhide, or delete abuse/spam |
| `instagram_insights` | how a post or the account performed, so "post what works" has numbers |
| `instagram_publishing_limit` | how much of the rolling 24h quota is left |
| `instagram_profile` | bio, follower and post counts |
| `instagram_mentions` | posts that tagged this account |

Two of these earn their keep in ways worth calling out. **`instagram_publishing_limit`** is checked
*before* generating media: Instagram caps API posts per rolling 24 hours (a carousel counts as one),
and a container you can't publish is a wasted Sora clip — if the quota is gone, the agent finishes
with `report_failure` rather than posting. **`instagram_insights`** uses a deliberately small default
metric set (`reach,likes,comments,saved,shares`): Meta retired `impressions`, `profile_views`,
`website_clicks` and non-Reels `video_views` in v21, so asking for them fails the whole call. Pass
`metrics=` to override, and a rejected name is reported back so the agent can pick another.

> **Scopes, and why one bad one breaks everything.** Meta rejects the *entire* login dialog if your
> app cannot request even a single scope you ask for — `Invalid Scopes: instagram_manage_insights` —
> which blocks connecting at all, **including publishing**, which doesn't need insights. The default
> asks for the full set:
>
> ```
> instagram_basic  instagram_content_publish  pages_show_list  pages_read_engagement
> business_management  instagram_manage_comments  instagram_manage_insights
> ```
>
> Both `instagram_manage_comments` and `instagram_manage_insights` need **App Review**. If your app
> hasn't been granted insights, the dialog refuses everything — set `INSTAGRAM_SCOPES` in `.env` to
> the list minus that scope (commas or spaces; it replaces the default outright):
>
> ```bash
> INSTAGRAM_SCOPES="instagram_basic instagram_content_publish pages_show_list pages_read_engagement business_management instagram_manage_comments"
> ```
>
> The bare minimum that can still publish is the first four. Check what your app actually has under
> **App Review → Permissions and Features** and match the list to it. An **already-connected account
> must be reconnected** for any scope change to take effect — until then those tools return a
> permissions error. Reconnect from **Accounts → Connect**.

> **"…must be granted before impersonating a user's page" (code 190).** Publishing acts *as the
> Facebook Page*, so the token AISMM stores has to be the **Page's** token, not your user token.
>
> **Several Instagram accounts? Connect them in ONE login.** A single **Connect** claims every Page
> the login administers, so three handles arrive as three accounts, each with its own page token.
> Tick every Page in the dialog and you are done in one round-trip.
>
> This matters because one Meta app plus one Facebook login holds a *single* grant, and authorising
> again **replaces** it. Connecting accounts one at a time — each with only its own Page selected —
> makes the second connect strip `pages_show_list` / `pages_read_engagement` from the grant the first
> account's page token was minted against. It keeps looking connected, publishes fine until its token
> is next used, then fails with code 190 while the newly added account works perfectly. That
> asymmetry (newest works, older ones broke) is the tell.
>
> AISMM now catches this: after every connect it re-checks the other accounts on that platform and
> warns on the spot if one just lost page access. **Accounts → Check permissions** settles any
> individual case — it asks Graph's `/debug_token` whether the stored token is a `PAGE` or a `USER`
> token, whether it's still valid, and exactly what was granted. A `USER` token is the direct cause
> of code 190 however complete its scope list looks. A connection that returns no page token at all
> is now refused at connect time rather than stored and left to fail later.

> **Replies are not covered by the publish mode.** `dry_run`/`approval` gate *posts*; a reply or a
> moderation action happens immediately. That's deliberate — an approval queue for comment replies
> would make them useless — but it means an instruction with comment duties acts on the live account
> even in `dry_run`. Keep that in mind when writing the brief.

### What the agent can do on X (Twitter)

The same shape as the Instagram surface, and likewise only present on runs that target an X account.

**Publishing** goes through the one gated `publish` tool. X takes **up to 4 images** in a post
(`asset_paths=[…]`) or **one video** — mixed sets are refused.

**A caption over 280 characters becomes a thread**, rather than being cut off. It splits on the
largest natural boundary that fits — paragraph, then sentence, then word — so nothing breaks
mid-word, and each post is numbered `n/m` (the counter is inside the 280, not added on top):

```
[154] Hearing loss is the third most common chronic condition in older adults, yet the
      average person waits seven years before seeking help.

      🤖 AI-generated 1/4
[142] That delay matters. Untreated hearing loss is linked to faster cognitive decline… 2/4
[185] The encouraging part: modern hearing aids are nothing like the beige devices…      3/4
[115] If conversations in restaurants have become work, that is worth a test…            4/4
```

Two details that took some care. **Media rides only on the first post** — otherwise X repeats the
image down the whole thread. And **the AI-disclosure label is moved to the first post**: it's
appended to the end of a caption, so on a thread it would land on `4/4`, invisible to anyone who
only meets `1/4` in their timeline — which is exactly the "first exposure" the label exists to
cover. If a later post in the chain fails, the ones already public are reported rather than lost.

The agent is told to write the whole thought in short paragraphs and let it split, rather than
pre-truncating or numbering by hand.

| Tool | What it's for |
|---|---|
| `x_recent_posts` | what the account already posted, with engagement counts — stops it repeating itself and shows its own voice |
| `x_mentions` | posts that mentioned the account |
| `x_reply_to_post` | answer one publicly, in the account's voice (posts immediately, like the Instagram reply tool) |
| `x_post_metrics` | impressions, likes, reposts, replies for one post |
| `x_profile` | bio, follower and post counts |
| `x_delete_post` | remove one of the account's own posts — a factual error, a duplicate |

> **X is pay-per-use — there is no free tier.** Since February 2026 you buy API credits up front and
> every call, read or write, spends them. An account with no credits gets **`402 Payment Required` on
> everything, posting included**:
>
> ```
> X API 402: Your enrolled account does not have any credits — this is BILLING, not a problem
> with the post. The X API is pay-per-use and your developer account has no credits left.
> Buy credits at https://console.x.com and retry; nothing in the post or the account
> connection needs changing.
> ```
>
> Every X call path spells that out, because httpx's own message — `Client error '402 Payment
> Required'` — leaves the agent guessing whether it did something wrong. It didn't, and no rewording
> of the post will help. Buy credits at [console.x.com](https://console.x.com).

### Stories, and the music question

**Stories are supported.** `publish(..., placement="story")` creates a `media_type=STORIES`
container. Two things to know:

- **A story carries no caption.** Graph ignores it, so any words have to be *in the image*.
- **A story is 9:16, not 4:5.** Instagram's 4:5 floor is a *feed* limit; applying it to a story pads
  a correct 1080×1920 image out to 1552×1920 and it publishes pillarboxed. Story placements now use
  their own aspect range (`story_min_image_ratio`), so a 9:16 image is left alone.

Because `/media` doesn't list stories, a story that fails at the last step is never reconciled the
way a feed post is — the [duplicate ledger](#the-same-post-never-goes-out-twice) guards that case
instead.

**Music is not supported, and cannot be.** Meta's Content Publishing API has **no parameter for
attaching a track from Instagram's audio library** — not for reels, not for stories. The container
takes `image_url`/`video_url`, `media_type`, `caption`, `is_carousel_item`, `upload_type`,
`is_ai_generated` and the branded-content fields, and nothing else. Third-party schedulers that
advertise "add music" either post a draft you finish in the app, or embed the audio in the file.

So the only audio that reaches a post is audio **already in the video file** — which is what
`generate_video` and `create_video_sequence` produce (Sora clips carry their own soundtrack, and
[merging](#video-longer-than-one-clip) preserves it). Adding a licensed track from Instagram's
library still has to be done by hand in the app afterwards.

### Media never carries over between runs

Asset files outlive the run that made them, but the agent cannot publish a path it merely *remembers*:
`publish` verifies every asset exists (locally or in blob storage) before calling the platform, and
refuses a `media_kind` with no file attached at all. Both errors name the tool to call instead.

The memory prompt reinforces it: record what was **published**, not what was created — a run that made
media but failed to post it has covered nothing, and writing it down as done makes the next run skip
work it never did.

### The same post never goes out twice

The agent's memory is model-written prose, so "did this already go out?" cannot depend on it. It did
once: a run recorded *"attempting panel X"*, published successfully, then ended without writing the
outcome — and the next scheduled run read that memory, concluded panel X was outstanding, and posted
the byte-identical image again.

Two deterministic guards now sit beside the publish-mode gate, where the AI disclosure lives:

- **A publish ledger.** Every successful live publish fingerprints its media (sha256 of the bytes
  plus the placement) into the account's `meta`, in code — **one fingerprint per item**, not one per
  post. A `publish` call is **refused** with `already_published` if *any* of its items is already in
  the ledger, and the message names which item, so a two-photo carousel cannot smuggle back a panel
  that already went out on its own. Keyed on the media, not the caption — the agent rewrites captions
  every run, so identical art with fresh words would otherwise slip through. Approving a staged post
  goes through the same check.
- **The account itself has the final say.** The ledger is only a record of what AISMM posted — it
  can't know you removed something in the Instagram app. So before a refusal actually blocks a post,
  the recorded media id is checked against the live account: **if the post is deleted _or archived_,
  the entry is dropped and the content publishes normally.** Archiving something is therefore a
  perfectly good way to tell AISMM "post this again". Only the refusal path pays for that lookup.
  (Archived detection scans the most recent 100 posts, since Graph has no `is_archived` field and
  archived posts are simply missing from the profile listing — beyond that depth the answer is
  "can't tell" rather than a guess.)
- **If that check can't complete** (rate limited, network trouble), the post goes out anyway. For
  sequential content — a comic posted panel by panel — a wrongly skipped item breaks the running
  order and the gap is permanent, whereas a duplicate is two taps to delete. Set
  `PUBLISH_DUPLICATE_GUARD_STRICT=1` to refuse instead, where an accidental duplicate costs more
  than a gap. A *confirmed* duplicate is refused either way, and an unverified refusal explicitly
  tells the agent **not** to advance its position, so the item is retried rather than dropped.
- **Reconciliation after an ambiguous failure.** When Instagram's `media_publish` fails *after* the
  container reached `FINISHED`, Meta already had the media and may have posted it anyway — the
  outcome is unknown, not failed. AISMM reads the account's recent posts and, if one carrying our
  caption appeared in the last 15 minutes, reports it as published with the real permalink instead of
  a failure the next run would "retry".

A legitimate re-post of the same media is blocked for 30 days; change the schedule or the media to
post again sooner.

A publish that got through *despite* a rate-limit error is recorded as **published** and still starts
the cooldown — the post landed, but the app is being throttled, so the next scheduled run must not
knock again.

#### Repairing runs from before this existed

Runs that published under a rate-limit error are stored as `failed` with a live post on the account,
and the ledger only knows about posts made after it was introduced. A run whose **caption** is shown
in the runs table (rather than an error) is one that got as far as publishing. To find and fix them:

```bash
python -m aismm.cli reconcile
```

It reads each Instagram account's recent posts, matches them to failed runs by caption, and prints
what it would change. Nothing is written until you add `--apply`:

```bash
python -m aismm.cli reconcile --apply
```

That marks those runs `published` with their real permalink and seeds the duplicate guard with their
media, so the next scheduled run cannot post them again. It publishes and deletes nothing.

### Rate limits and "action is blocked"

Meta refuses some posts for **volume** rather than content — `code=4 Application request limit
reached`, or the same thing worded as *"action is blocked — we restrict certain activity to protect
our community"* (`error_subcode=2207051`). Retrying soon makes it worse: Meta extends these blocks
when an app keeps knocking.

So a volume refusal is handled differently from a content error:

- it is raised as a distinct `RateLimited`, never retried inside the run;
- the **account is put in a cooldown**, and the orchestrator skips subsequent `live` runs for that
  account *before* they start — no point browsing, downloading and converting media that will be
  refused at the last step. `dry_run` still runs, since it touches no API;
- the failure names the likely cause: if the instruction fires very often, the message says so;
- **the cooldown escalates on repeated refusals** — 1h, 2h, 4h, … doubling up to a 24h cap. A flat
  60-minute cooldown against an `every 1h` schedule barely helps if the real block outlasts an hour:
  the very next scheduled fire lands right as the cooldown clears and knocks again, which extends the
  block further. A clean publish that goes through with no rate-limit signal at all resets the streak,
  so a single bad hour doesn't permanently escalate every future isolated refusal;
- **but only a failed publish counts as a strike.** When Instagram returns a rate-limit error *and
  publishes the post anyway* (the reconciled case above), that is a success with a noisy error, not a
  refusal: it sets the base 1h pause and does **not** escalate. Counting those meant four consecutive
  successful posts climbed 60 → 120 → 240 minutes, throttling a perfectly healthy account toward
  silence. Such a post leaves the streak parked — not reset, since the error was real, so a later
  genuine refusal resumes from where it was.

```
Publishing cooldown for apadana.audiology.clinic (instagram): 60 minutes (strike 1) — instagram rate limit
… — instagram is refusing posts for volume reasons, not because of this content. Publishing is
paused for 60 minutes. This instruction runs 'every 1h' — that is far more often than a single
account can publish. Lengthen the schedule.

Publishing cooldown for apadana.audiology.clinic (instagram): 120 minutes (strike 2) — instagram rate limit
… Publishing is paused for 120 minutes. This is strike 2 — the cooldown doubles each time this
recurs, capped at 24h. This instruction runs 'every 1h' — that is far more often than a single
account can publish. Lengthen the schedule.
```

A post that landed despite the error looks different — note **no strike**, so it stays at the base
duration however many times it happens:

```
Publish reconciled for genaicomicbook: Instagram errored (…403…) but the post is live at …
Publishing cooldown for genaicomicbook (instagram): 60 minutes (no strike — post landed)
```

> **A schedule of `every 1h` is 24 posts a day to one account.** That is the usual cause. Instagram's
> published cap is 50 API posts per rolling 24 hours, but the *app* request limit and its
> spam heuristics bite well before that. A few posts a day is a realistic ceiling; use
> `instagram_publishing_limit` to see the remaining quota.

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

### Schedules

Times of day, weekday filters, intervals and cron — combined freely. Everything is UTC, and the
instruction form shows a **readback** of how your text was understood.

| You type | It means |
|---|---|
| `09:00` | daily at 09:00 UTC |
| `9am, 6pm` | twice a day |
| `09:00 mon-fri` | weekdays only |
| `09:30 and 17:45 weekends` | two times, Saturday and Sunday |
| `every 6h` · `30m` · `every 2 days` | intervals (floor: 1 minute) |
| `hourly` · `daily` · `weekly` · `@daily` | named cadences |
| `0 */4 * * *` | raw cron still works |
| `every 6h; 08:00 mon` | mix them — `;` or a newline starts a new rule |

An instruction can therefore produce **several triggers**, and the scheduler registers one job per
trigger. A schedule it cannot parse logs a warning and never fires, rather than guessing — a bare
`6` is ambiguous (06:00? every 6 hours?) and is refused for that reason. Editing an instruction
live-reschedules its jobs.

Note that intervals take a **single count and unit**: `every 90 minutes` (or `90m`) works,
`every 1.5h` and `every 1h30m` do not — and an unparseable schedule never fires, so check the
readback.

#### When does an interval actually start?

An `every Nh` schedule counts from a fixed **anchor**, shown as **Next run** on the instructions page:

- the instruction's optional **Starts** field, if you set one;
- otherwise the moment the instruction was **created**.

So `every 1h` on an instruction created at 08:12 fires at 09:12, 10:12, … — not on the hour. Set
*Starts* to `09:00` to put it on the hour instead, or to a future time to delay the first run.

This anchoring matters for a reason that is not obvious: the scheduler rebuilds **every** job from
scratch whenever any instruction is saved, and on every service restart. Without a fixed anchor an
interval would re-base to that moment each time, quietly pushing the next run a full interval into
the future — an instruction on `every 6h` in a frequently-edited deployment could go a long time
without ever firing, with nothing in the log to say so. The anchor is what makes the phase survive.

For a time-of-day or cron schedule there is no drift to fix, and *Starts* simply delays the first
fire until that moment.

#### "Next run" means the next *post*, not the next tick

A `live` instruction whose account is in a [publishing cooldown](#rate-limits-and-action-is-blocked)
is skipped before it does any work, so the scheduler's next fire time can be a run that publishes
nothing. **Next run** therefore shows the first fire that will actually get past the cooldown, with
the reason underneath:

```
2026-07-31 10:46 UTC
publishing paused until 10:39 UTC — earlier fires are skipped
```

Only `live` mode is affected — `dry_run` calls no platform API and runs regardless — and only when
*every* target account is blocked, since one free account still makes the fire worth firing.

A **Clear cooldown** button appears next to *Run now* while any of the instruction's accounts is
paused, so you can override the wait — after fixing the schedule, say, or when you know the block
has lifted. It asks for confirmation first, because it is an override rather than a fix: if the
platform is still blocking, posting again now is exactly what extends the block. The **strike count
is deliberately kept**, so a refusal right after clearing resumes the escalation (2h, 4h, …) instead
of restarting at 60 minutes — otherwise a clear-then-refused loop would never back off at all.

### Which tools an instruction may use

Every registered tool is available by default. The instruction form has a **Tools the agent may
use** picker — a checkbox dropdown with a filter, grouped by capability (Essentials, Continuity,
Research, Media, Instagram) — for narrowing that:

- it keeps an instruction on task (a text-only account has no business calling Sora), and
- it cuts the number of choices a smaller model has to weigh, which matters on the mini models.

`publish` and `report_failure` are always on whatever you tick, since [a run has to be able to
end](#a-run-ends-with-publish-or-report_failure). The filter and the *Select all* / *Clear* links
compose, so typing `instagram` then *Clear* switches off just those eight.

Leaving everything ticked stores "all" rather than a list of today's names, so a tool added in a
later version is picked up automatically. Unticking everything is *not* the same thing — it stores
just the two terminal tools.

### Token expiry and automatic refresh

Most platforms issue a short-lived access token — X's lasts about **two hours**, YouTube's one —
alongside a long-lived refresh token. AISMM refreshes automatically: before any call to a platform it
checks the recorded expiry and, if the token is within ten minutes of dying, spends the refresh token
and stores the new pair. Nothing on a schedule breaks overnight because of it.

The **Accounts** page shows each token's state — "in 43 minutes", "expired 2 hours ago" — and whether
it *renews automatically*. An account marked **reconnect** has no usable refresh token, which is the
one case that needs you: reconnect it from that page. Instagram page tokens carry no expiry at all
and are left alone.

If a refresh is refused (a revoked grant, a password change, an app whose credentials were replaced)
the stored token is used anyway, so the platform's own error is what you see — a clear "401
Unauthorized, reconnect the account" rather than an unexplained crash earlier in the run.

### Retrying a failed run

A failed run offers two different repairs, and picking the right one matters.

**Publish this again** — the run's media and caption were fine and only the *publish* was refused:
a rate limit, an expired token, X out of API credits. It sends the exact assets that run already
produced, with the caption editable, straight to the publish gate. **No agent, no model call,
nothing regenerated** — so nothing to pay for or wait for twice, and the content is the one you
already reviewed rather than a fresh render of the same brief. It is expanded by default on a failed
run that has media or a caption. The gate, the AI label, the cooldown and the duplicate guard all
still apply, so a post that actually went out won't be duplicated. If an asset has since been
deleted the form refuses rather than publishing whatever is at that path now, and points you at the
agent retry.

**Re-run the agent** — the *content* was the problem. Every run records the exact kickoff it was
given, so when one fails for a reason the prompt itself caused — a stale memory position, a brief
pointing at the wrong page — the prompt comes pre-filled and is editable. This regenerates the
media (a new Sora clip or image), costing time and API spend.

It starts a **new** run against the same instruction and account; the failed one is left exactly as
it is, since that is the evidence. The edited text is sent **verbatim** — memory and the operator
note are not re-inlined — so what is in the box is precisely what the agent receives. Empty the box
to rebuild the prompt from the instruction as it stands now.

A retry is an ordinary run in every other respect: the publish-mode gate, the per-account lock, the
cooldown check and the duplicate guard all apply. On a `live` instruction the form says so, because
a successful retry posts for real.

### Files attached to an instruction

An instruction can carry files, available to **every run** of it, uploaded on its edit page (25MB each):

| Purpose | What happens |
|---|---|
| **context** | A PDF or image is sent to the model **directly as a file** (the Responses API's `input_file`/`input_image`), so it reads the real layout, tables and pixels — not our own extraction. Plain text (`txt`, `md`, `csv`, `json`, `html`) is inlined. A file too large to attach, or a deployment that rejects file parts, falls back to the text extracted once on upload, with `read_attachment("voice.pdf")` giving the agent the rest. |
| **reference** | The image's `asset_path` is put in front of the agent to pass to `generate_image` (`reference_asset_paths`) or a video sequence, so a look can be held across posts. Never sent to the text model itself. |

A brand-voice PDF, a price list, a palette swatch, a product photo to match. Text is still
extracted on upload as a fallback (best-effort, never blocks the upload — a scanned PDF with no
text layer stores fine and the flash message says so). Each file inlines up to 12MB, with a 28MB
budget shared across all of an instruction's attachments in one run; anything over that falls back
to its extracted text instead of being dropped. Deleting an instruction deletes its attachments.

### The Runs page

Built to stay usable as the table grows — filtering, sorting and paging all happen in the **store**,
so the page loads one page of rows however many runs exist.

- **Search** across captions, errors, run logs, published URLs, *and* the instruction's name (the run
  row only stores its id, but that isn't what you'd type).
- **Filter** by status, instruction, and account; combine freely.
- **Sort** by when / instruction / account / status, ascending or descending — click a column header.
  Sort links keep the active filters.
- **Page** at 25 / 50 / 100 / 200 per page.

Every row links to a **run detail page**. Under *"What the agent was told"* it has expandable
sections for the **exact kickoff prompt that run received** — brief + memory + note + attachments +
platform rules, stored on the run itself, so a failure can be debugged from what the agent was told
rather than from what the instruction says now — plus the current brief, memory, note, attachment list
and system prompt for comparison. Below that: the full log and error, the caption, the media it
produced, the staged posts it created, its instruction (with publish mode) and account, one-click
links to "all runs for this instruction / account / status", and the `journalctl` incantation for the
full service log:

```bash
journalctl -u aismm.service | grep <run-id-prefix>
```

which works because **every log line carries the id of the run that produced it**, in a column after
the level:

```
16:02:32 INFO    3332fc11  aismm.tools.sequence   Shot 1/4 done (create, 4.0s, 1831834 bytes)
16:05:13 INFO    6754a8a8  aismm.tools.image      Generating image: {'model': 'gpt-image-2', …}
16:07:03 INFO    3332fc11  aismm.video            Merged 4 clip(s) into 2685041 bytes at 720x1280
```

That matters because runs overlap routinely — a dashboard **Run now** next to a scheduled fire — and
their lines interleave. Anything logged outside a run shows `-`.

### On a phone

The dashboard is responsive — it's meant to be usable from a phone when a run fails at an awkward
hour. What that took, measured at a 375px viewport:

| Problem | Fix |
|---|---|
| page scrolled sideways by 468px | wide tables scroll inside a `.table-scroll` box; the page never does |
| nav clipped off the right edge | the topbar wraps and the nav scrolls horizontally |
| form controls at 13–14px | 16px on any touch device — below that, iOS Safari zooms in on focus and never zooms back |
| filters squeezed onto one line | stacked full-width below 720px |
| sticky sidebar ate the viewport | static on short screens |
| run ids / asset paths widened the page | `overflow-wrap: anywhere` |
| small tap targets | 44px minimum |

Verified at 375 / 768 / 1280px across every page: zero horizontal overflow.
[`tests/test_responsive.py`](tests/test_responsive.py) guards the essentials — notably that every
table stays wrapped and every control stays at 16px on touch.

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

**A broken deploy no longer takes the service down.** Before restarting anything,
[`scripts/preflight.py`](scripts/preflight.py) checks that the new code can actually boot — both
`Store` backends concrete, every module importable, credentials present for the selected LLM
provider. If it fails, the deploy aborts and the **running service is left untouched**:

```
PREFLIGHT FAILED:
  ✗ AzureStore does not implement 2 method(s) declared in Store: count_runs, get_run.
```

Run it any time with `python scripts/preflight.py`; `SKIP_PREFLIGHT=1` overrides it.

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
