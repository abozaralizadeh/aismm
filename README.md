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
- [The public-media-URL caveat (Instagram)](#the-public-media-url-caveat)
- [Running it](#running-it) · [VS Code debugging](#debugging-in-vs-code) · [Deploying (systemd)](#deploying-on-a-server-systemd)
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
APIM_BASE_URL=https://<apim>.azure-api.net/<openai-path>
APIM_SUBSCRIPTION_KEY=...
APIM_KEY_HEADER=api-key           # or Ocp-Apim-Subscription-Key, per your APIM policy
APIM_API_VERSION=2025-04-01-preview
AZURE_OPENAI_MODEL=gpt-4o
```

Both paths build one shared `OpenAIResponsesModel` and register it as the SDK default (so the hosted
`WebSearchTool` routes through it too). Wiring lives in [`aismm/llm.py`](aismm/llm.py).

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
| `DASHBOARD_HOST` / `DASHBOARD_PORT` / `DASHBOARD_BASE_URL` / `FLASK_SECRET_KEY` | Dashboard. |
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

## The public-media-URL caveat

**Instagram fetches your media from a public URL** — it does not accept a direct binary upload. AISMM
serves generated assets at `<DASHBOARD_BASE_URL><REVERSE_PROXY_PREFIX>/assets/<file>`, so
**`DASHBOARD_BASE_URL` must be a public HTTPS address** for Instagram to work (use an ngrok tunnel
locally, or deploy the dashboard). If it points at `localhost`/`127.0.0.1`, the Instagram
integration raises a clear error instead of failing silently.

X, YouTube, and TikTok upload the bytes directly, so they work without a public URL. For a fully
cloud-hosted setup, implement the Azure Blob adapter (`aismm/store/azure_store.py`) and serve media
from Blob with a public/SAS URL.

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
https URL — OAuth callbacks and Instagram's media fetch both depend on it. The dashboard has **no
authentication of its own**; do not expose it directly to the internet.

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

## Security

- OAuth tokens are **encrypted at rest** (Fernet). Keep `tokens.key` and `.env` out of version
  control (both are git-ignored). Set `AISMM_TOKEN_KEY` to pin the key across machines.
- Never commit real credentials. The dashboard has no auth of its own — run it on a trusted network
  or put it behind your own reverse proxy / auth.
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
