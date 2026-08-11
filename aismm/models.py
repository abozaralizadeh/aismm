"""Persistence models + enums (SQLModel).

Four entities drive the whole system:

* ``Account``      — a connected social profile (platform + encrypted OAuth tokens).
* ``Instruction``  — a dashboard-authored directive: a brief + selected accounts +
                     schedule + publish mode. This is what the scheduler fires.
* ``Run``          — one execution of an instruction against one account.
* ``StagedPost``   — a prepared-but-not-yet-live post (dry-run preview or an
                     approval-queue item awaiting a human click in the dashboard).
* ``InstructionState`` — an instruction's carry-over memory (agent-written) and
                     note (human-written); see the class docstring.
* ``PlatformApp``  — a developer app's OAuth credentials, editable in the
                     dashboard so one deployment can serve several apps/brands.

Plus a ``Lock`` table used for single-flight de-duplication (ported from the
SandBox ``_try_acquire_lock`` pattern) so a schedule firing twice never
double-posts.
"""
from __future__ import annotations

import enum
import json
import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PlatformName(str, enum.Enum):
    instagram = "instagram"
    twitter = "twitter"
    youtube = "youtube"
    tiktok = "tiktok"
    linkedin = "linkedin"
    facebook = "facebook"
    reddit = "reddit"


class PublishMode(str, enum.Enum):
    """How autonomously an instruction is allowed to publish.

    Configured per-instruction in the dashboard. The publish tool enforces this
    in code, so the agent always "decides + publishes" but the real side effect
    is gated here.
    """

    dry_run = "dry_run"    # prepare a preview only; never call the platform API
    approval = "approval"  # prepare + queue; a human approves in the dashboard
    live = "live"          # publish immediately to the real platform API


class MediaPref(str, enum.Enum):
    auto = "auto"      # let the agent decide (text / image / video)
    video = "video"    # prefer a generated Sora video
    image = "image"    # prefer a generated image
    text = "text"      # text-only


class InstructionTask(str, enum.Enum):
    """What an instruction's run is FOR.

    ``publish`` is the original behaviour: research, make media, publish one new
    post. ``engage`` is a different job entirely — read the account's new
    comments/mentions and reply in its voice — so it swaps in an engagement
    prompt and a non-failure terminal tool (``finish_engagement``) instead of
    ``publish``. The scheduler fires both the same way; the difference is which
    prompt/tools the agent gets (see agent/manager_agent.py).

    ``auto`` lets the AGENT decide, per run, whether to publish or engage — it is
    given BOTH tool sets and BOTH terminals and picks the one that fits the brief
    and the account's current state (are there new comments worth answering?).
    This deliberately relaxes the disjoint-terminal safety rule the other two
    rely on: the operator has explicitly asked the agent to choose, so offering
    ``publish`` on a run that turns out to be engagement is the intent, not a leak.

    ``outreach`` is the OUTBOUND mirror of ``engage``: rather than answering
    comments on the account's OWN posts, it goes and finds OTHER people's content
    (by keyword / hashtag / subreddit / @account, from ``engagement_targets`` or
    inferred from the brief) and engages with it — a reply or a like, through the
    same ``publish_mode`` gate. It gets its own prompt, its own search tools, and
    the ``finish_engagement`` terminal (the outcome shape is the same: replied /
    staged / skipped). ``engage`` and ``outreach`` never both run in one job.
    """

    publish = "publish"     # create + publish one post (the default)
    engage = "engage"       # respond to comments/mentions on the account's own posts
    outreach = "outreach"   # find and engage OTHERS' content (the follower engine)
    auto = "auto"           # agent decides publish vs engage per run


class RunStatus(str, enum.Enum):
    running = "running"
    published = "published"
    staged = "staged"        # dry-run preview or queued for approval
    rejected = "rejected"    # a queued post the operator declined
    skipped = "skipped"      # e.g. locked by a concurrent run
    failed = "failed"


class StagedStatus(str, enum.Enum):
    preview = "preview"                    # dry-run: informational only
    pending_approval = "pending_approval"  # waiting for a dashboard click
    approved = "approved"
    rejected = "rejected"
    published = "published"


class WorkspaceRole(str, enum.Enum):
    owner = "owner"     # + manage membership, connect accounts, delete the workspace
    member = "member"   # author instructions, run them, approve posts


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
class Workspace(SQLModel, table=True):
    """A silo: its own social accounts, instructions, runs and staged posts.

    Nothing is visible across workspaces. There is only ONE kind of workspace —
    every one starts private to whoever created it and becomes shared the moment
    they add a member. A separate "personal" kind that could also be shared was
    a distinction without a difference, and it made "can I share this?" look
    like it depended on how the workspace was created.

    ``claims_unassigned`` marks the ONE workspace that also owns rows carrying no
    ``workspace_id`` — content written before workspaces existed. It is set on
    the workspace the first signed-in operator inherits, and on nothing else.
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str = ""
    claims_unassigned: bool = False
    created_by: str = ""                     # the identity that created it
    created_at: datetime = Field(default_factory=_now)


class WorkspaceMember(SQLModel, table=True):
    """One identity's membership of one workspace.

    Keyed by the email the identity provider returns, lowercased — there is no
    local user table, because there are no local accounts to keep: identity comes
    from SSO, and with SSO off the dashboard is unauthenticated anyway.
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    workspace_id: str = Field(index=True)
    email: str = Field(index=True)
    role: WorkspaceRole = WorkspaceRole.member
    display_name: str = ""
    created_at: datetime = Field(default_factory=_now)


class Account(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    workspace_id: str = Field(default="", index=True)
    platform: PlatformName
    handle: str = ""                         # @name / channel title (display only)
    external_id: str = ""                    # platform user/page/channel id
    # Encrypted (Fernet) token blobs — never store plaintext tokens.
    access_token_enc: str = ""
    refresh_token_enc: str = ""
    expires_at: datetime | None = None
    meta_json: str = "{}"                    # platform-specific bits (ig page id, etc.)
    created_at: datetime = Field(default_factory=_now)

    @property
    def meta(self) -> dict:
        try:
            return json.loads(self.meta_json or "{}")
        except json.JSONDecodeError:
            return {}

    def set_meta(self, value: dict) -> None:
        self.meta_json = json.dumps(value or {})


class Instruction(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    workspace_id: str = Field(default="", index=True)
    name: str
    brief: str = ""                          # the persona / directive / theme text
    account_ids_json: str = "[]"             # selected accounts (multi-select)
    schedule: str = ""                       # cron ("0 9 * * *") or interval ("every 6h")
    # Optional: the interval's phase anchor / a "don't fire before" gate (UTC).
    # Unset, an interval schedule anchors to created_at instead of "now" at every
    # (re)registration — see aismm/schedules.py for why that distinction matters.
    schedule_start_at: datetime | None = Field(default=None)
    # Which agent tools this instruction may use. Empty = all of them (the
    # default). Narrow it to keep an instruction on task and to cut the number of
    # choices a smaller model has to weigh. `publish`/`report_failure` are always
    # available regardless — a run has to be able to end.
    tools_json: str = "[]"
    # Which LLM connection this instruction's runs use (``LLMConfig.id``). Empty
    # means the deployment default (the ``.env`` model, sentinel id ``"env"``).
    # Resolved + access-checked per run in agent/manager_agent; a run whose
    # selected connection is not accessible fails with a clear "no LLM" error.
    llm_config_id: str = ""
    # Which image / video (Sora) generation connection this instruction's runs use
    # (``ProviderConfig.id``). Empty means the deployment default (the ``.env``
    # sentinel). Unlike the LLM these are OPTIONAL: an unconfigured default just
    # leaves the generation tool absent, so there is no "no connection" failure.
    image_config_id: str = ""
    video_config_id: str = ""
    # What this instruction's runs DO. ``publish`` (default) creates a post;
    # ``engage`` responds to comments/mentions. ``publish_mode`` still applies to
    # both — for an engage run it gates how replies go out (preview / approval /
    # live), the same three-way gate posts use.
    task_type: InstructionTask = InstructionTask.publish
    # Where an OUTREACH run looks for others' content to engage with: a free-text
    # list of keywords, #hashtags, r/subreddits and @accounts (comma- or
    # newline-separated). Empty is fine — an outreach run then infers targets from
    # the brief. Ignored by publish/engage/auto runs. Parsed by
    # aismm/targets.py into typed buckets the per-platform search tools consume.
    engagement_targets: str = ""
    publish_mode: PublishMode = PublishMode.dry_run
    media_pref: MediaPref = MediaPref.auto
    # Label this instruction's posts as AI-generated. On by default (EU AI Act
    # Art. 50 + platform rules — see aismm/disclosure.py); turn it off per
    # instruction when you have a reason to.
    disclose_ai: bool = True
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def account_ids(self) -> list[str]:
        try:
            return list(json.loads(self.account_ids_json or "[]"))
        except json.JSONDecodeError:
            return []

    def set_account_ids(self, ids: list[str]) -> None:
        self.account_ids_json = json.dumps(list(ids or []))

    @property
    def tools(self) -> list[str]:
        """Selected tool names; empty list means "all tools"."""
        try:
            return list(json.loads(self.tools_json or "[]"))
        except json.JSONDecodeError:
            return []

    def set_tools(self, names: list[str]) -> None:
        self.tools_json = json.dumps(list(names or []))

    @property
    def parsed_targets(self):
        """``engagement_targets`` split into typed buckets (see aismm/targets.py)."""
        from .targets import parse_targets
        return parse_targets(self.engagement_targets)


class Run(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    workspace_id: str = Field(default="", index=True)
    instruction_id: str = Field(index=True)
    account_id: str = Field(index=True)
    status: RunStatus = RunStatus.running
    caption: str = ""
    asset_path: str = ""
    # The FULL media set and placement this run tried to publish, so a failed
    # publish can be sent again exactly as it was — without re-running the agent
    # and paying for a fresh Sora clip or image to fix, say, a billing error.
    # asset_path keeps the first for previews and backwards compatibility.
    asset_paths_json: str = "[]"
    placement: str = "feed"
    external_url: str = ""                    # published permalink, when live
    # The platform's own id for the published post (tweet id, IG media id, YouTube
    # video id, …). external_url is for a human to click; this is what the
    # performance feedback loop polls the platform's metrics API with later.
    external_id: str = ""
    # How the published post has performed, refreshed after the fact by
    # orchestrator.refresh_metrics. A normalized dict (likes/comments/…); the exact
    # keys depend on the platform. metrics_updated_at is None until first polled.
    metrics_json: str = "{}"
    metrics_updated_at: datetime | None = None
    error: str = ""
    log: str = ""                            # short human-readable trace of the run
    # The kickoff prompt this run actually received — brief + memory + note +
    # platform rules, as composed at the time. Kept so a failed run can be
    # debugged from what the agent was told, not from what the instruction says now.
    prompt: str = ""
    created_at: datetime = Field(default_factory=_now)

    @property
    def asset_paths(self) -> list[str]:
        try:
            paths = list(json.loads(self.asset_paths_json or "[]"))
        except json.JSONDecodeError:
            paths = []
        return paths or ([self.asset_path] if self.asset_path else [])

    def set_asset_paths(self, paths: list[str]) -> None:
        self.asset_paths_json = json.dumps(list(paths or []))

    @property
    def metrics(self) -> dict:
        try:
            return json.loads(self.metrics_json or "{}")
        except json.JSONDecodeError:
            return {}

    def set_metrics(self, value: dict) -> None:
        self.metrics_json = json.dumps(value or {})


class StagedPost(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    workspace_id: str = Field(default="", index=True)
    instruction_id: str = Field(index=True)
    account_id: str = Field(index=True)
    run_id: str = ""
    caption: str = ""
    asset_path: str = ""
    media_kind: str = "text"                 # text | image | video
    # A carousel has several files; asset_path keeps the first for previews.
    asset_paths_json: str = "[]"
    placement: str = "feed"                  # feed | story | reel
    # What KIND of staged action this is. ``post`` (default) is a new post whose
    # media/caption sit in the fields above. ``reply`` is an engagement reply:
    # the reply text reuses ``caption`` (``media_kind="text"``), and the target
    # it answers is described by the three fields below. Sharing one table keeps
    # a single approval queue and one Approve/Reject path (orchestrator).
    action_type: str = "post"                # post | reply
    target_type: str = ""                    # comment | mention | reply | dm
    target_id: str = ""                      # id of the comment/post/message being answered
    # Where a reply is SENT when that differs from what it answers. For a comment
    # the two are the same (reply to the comment id), so this stays "". For a DM
    # they differ: target_id is the inbound MESSAGE id (the ledger dedupe key) and
    # this is the CONVERSATION / RECIPIENT id the reply is delivered to — so the
    # Approve button (orchestrator) knows where to send it. See engagement.py.
    target_conversation: str = ""
    target_excerpt: str = ""                 # what we're replying to, shown to the reviewer
    status: StagedStatus = StagedStatus.preview
    external_url: str = ""
    created_at: datetime = Field(default_factory=_now)
    # An approval-mode post the operator chose to publish LATER: status becomes
    # ``approved`` and this holds when. A per-minute scheduler sweep publishes it
    # once due (orchestrator.publish_due_staged). None = publish on approval.
    publish_at: datetime | None = None

    @property
    def asset_paths(self) -> list[str]:
        try:
            paths = list(json.loads(self.asset_paths_json or "[]"))
        except json.JSONDecodeError:
            paths = []
        return paths or ([self.asset_path] if self.asset_path else [])

    def set_asset_paths(self, paths: list[str]) -> None:
        self.asset_paths_json = json.dumps(list(paths or []))


class PlatformApp(SQLModel, table=True):
    """A developer app's OAuth credentials, managed from the dashboard.

    Credentials used to live only in ``.env``, which allowed exactly one app per
    platform and required a redeploy to change. Several apps per platform are
    normal — a separate Meta app per brand, a second X app for a client — and
    each connected account remembers which app authorised it.

    ``.env`` still works: when no app row exists for a platform, the
    ``PlatformCreds`` from settings are used, so existing deployments keep
    running untouched.

    The secret is Fernet-encrypted by the store, exactly like account tokens.
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    # OAuth client credentials are as sensitive to tenant boundaries as the
    # accounts they connect. They must never appear as a connection choice in
    # another workspace.
    workspace_id: str = Field(default="", index=True)
    platform: PlatformName = Field(index=True)
    name: str = ""                           # label, e.g. "Brand A — Meta app"
    client_id: str = ""
    client_secret_enc: str = ""
    extra_json: str = "{}"                   # per-platform extras (X's API key/secret)
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)

    @property
    def extra(self) -> dict:
        try:
            return json.loads(self.extra_json or "{}")
        except json.JSONDecodeError:
            return {}

    def set_extra(self, value: dict) -> None:
        self.extra_json = json.dumps(value or {})

    @property
    def label(self) -> str:
        return self.name or f"{self.platform.value} app {self.id[:8]}"


# The deployment ``.env`` LLM is exposed as a single sentinel ``LLMConfig`` row
# so it slots into the same picker, sharing, and resolution paths as any
# user-created connection — its credentials still come from ``settings.llm``.
ENV_LLM_ID = "env"

# The deployment ``.env`` image and video (Sora) connections are single sentinel
# rows too, mirroring ENV_LLM_ID: their config/secret fields are empty and
# resolution reads ``settings.image`` / ``settings.sora``. Owner-owned, so nobody
# else can reshare them. See ``ProviderConfig`` and ``llm_access.env_provider_config``.
ENV_IMAGE_ID = "env-image"
ENV_VIDEO_ID = "env-video"


class LLMConfig(SQLModel, table=True):
    """A user-managed LLM connection (Azure OpenAI or an APIM gateway).

    Everyone can add PRIVATE connections and pick one per instruction. Sharing:

    * ``shared_with_workspace`` — any creator may share their own connection with
      the members of ``workspace_id`` (they see the NAME only, never the secret).
    * ``shared_with`` (emails) — ONLY the site owner may share a connection with
      specific people, across workspaces (again, NAME only).

    The deployment ``.env`` model is a single sentinel row (``id == ENV_LLM_ID``,
    ``provider == "env"``, owned by the site owner): its config fields are empty
    and resolution reads ``settings.llm``. It is not editable/deletable, and
    since only the owner may set ``shared_with`` on their own rows, nobody else
    can reshare it.

    Secrets are Fernet-encrypted by the store, exactly like account tokens and
    ``PlatformApp`` credentials — the ``_enc`` columns hold ciphertext only.
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    workspace_id: str = Field(default="", index=True)   # where it was created
    created_by: str = Field(default="", index=True)     # lowercased identity that owns it
    name: str = ""                           # label shown to everyone with access
    provider: str = "azure"                  # "azure" | "apim" | "env"
    model: str = ""                          # deployment name
    # Azure direct
    azure_endpoint: str = ""
    azure_api_key_enc: str = ""              # secret
    azure_api_version: str = "2025-04-01-preview"
    # APIM gateway
    apim_base_url: str = ""
    apim_subscription_key_enc: str = ""      # secret
    apim_key_header: str = "api-key"
    apim_api_version: str = "2025-04-01-preview"
    # Sharing
    shared_with_workspace: bool = False      # any creator may set (name-only to members)
    shared_with_json: str = "[]"             # list of emails; ONLY the owner may set
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)

    @property
    def shared_with(self) -> list[str]:
        try:
            got = json.loads(self.shared_with_json or "[]")
            return [str(e).strip().lower() for e in got if str(e).strip()]
        except (json.JSONDecodeError, TypeError):
            return []

    def set_shared_with(self, emails: list[str]) -> None:
        cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
        self.shared_with_json = json.dumps(cleaned)

    @property
    def is_env(self) -> bool:
        return self.provider == "env" or self.id == ENV_LLM_ID

    @property
    def label(self) -> str:
        return self.name or (f"Deployment default" if self.is_env
                             else f"{self.provider} · {self.model or '?'}")


# The sentinel id for each provider kind's ``.env`` default, keyed by ``kind``.
_ENV_PROVIDER_IDS = {"image": ENV_IMAGE_ID, "video": ENV_VIDEO_ID}


class ProviderConfig(SQLModel, table=True):
    """A user-managed IMAGE or VIDEO (Sora) generation connection.

    The same sharing model as :class:`LLMConfig` (own / workspace-shared /
    owner-people-shared, ``.env`` sentinel as the deployment default), but for the
    two generation providers instead of the chat model. One table serves both
    ``kind`` values because their shapes differ — image is a single endpoint/key/
    model, video is a *pool* of them — so the non-secret fields live in a
    ``config_json`` blob and the secret(s) in a single Fernet-encrypted
    ``secrets_enc`` blob rather than fixed columns. ``llm_access.is_owned`` /
    ``can_select`` operate on this unchanged (they only touch the sharing fields).

    Unlike the LLM, image/video are OPTIONAL: an instruction that selects none
    falls back to the deployment default, and an unconfigured default just leaves
    the generation tool absent — there is no "no connection" run failure.
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    workspace_id: str = Field(default="", index=True)   # where it was created
    created_by: str = Field(default="", index=True)     # lowercased identity that owns it
    kind: str = "image"                      # "image" | "video"
    name: str = ""                           # label shown to everyone with access
    config_json: str = "{}"                  # non-secret settings (endpoint, model, …)
    secrets_enc: str = ""                    # Fernet-encrypted JSON of secret fields
    # Sharing (identical to LLMConfig, so the shared ACL helpers work unchanged)
    shared_with_workspace: bool = False      # any creator may set (name-only to members)
    shared_with_json: str = "[]"             # list of emails; ONLY the owner may set
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)

    @property
    def shared_with(self) -> list[str]:
        try:
            got = json.loads(self.shared_with_json or "[]")
            return [str(e).strip().lower() for e in got if str(e).strip()]
        except (json.JSONDecodeError, TypeError):
            return []

    def set_shared_with(self, emails: list[str]) -> None:
        cleaned = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
        self.shared_with_json = json.dumps(cleaned)

    @property
    def config(self) -> dict:
        try:
            got = json.loads(self.config_json or "{}")
            return got if isinstance(got, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_config(self, values: dict) -> None:
        self.config_json = json.dumps(values or {})

    @property
    def is_env(self) -> bool:
        return self.id == _ENV_PROVIDER_IDS.get(self.kind)

    @property
    def provider(self) -> str:
        """A stable string for the shared ACL helpers' ``is_env`` duck-typing.

        ``llm_access`` checks ``config.is_env`` (a property here), so this only
        needs to exist for parity; it reports ``"env"`` for a sentinel row.
        """
        return "env" if self.is_env else self.kind

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        if self.is_env:
            return "Deployment default"
        return f"{self.kind} · {self.config.get('model') or '?'}"


class UserProfile(SQLModel, table=True):
    """A durable record of a signed-in identity — the app has no user table
    otherwise (identity is just the SSO email). Written on login so the owner's
    Admin page can show last-login and attribute usage to a person.
    """

    email: str = Field(primary_key=True)     # lowercased SSO identity
    display_name: str = ""
    last_login_at: datetime = Field(default_factory=_now)
    # The last time this identity actually interacted with the site (any request),
    # not just when the session began — updated (throttled) on activity so Admin
    # can tell "signed in a week ago" from "still using it right now".
    last_active_at: datetime = Field(default_factory=_now)
    created_at: datetime = Field(default_factory=_now)


class AttachmentPurpose(str, enum.Enum):
    """What an uploaded file is FOR, which decides how it reaches the model."""

    context = "context"      # given to the agent to read — natively (PDF/image) or as text
    reference = "reference"   # an image handed to the image/video generator


class InstructionFile(SQLModel, table=True):
    """A file attached to an instruction, available to every run of it.

    Two uses, chosen per file:

    * ``context`` — a brief, a style guide, a price list, a PDF of source
      material. A PDF or image is sent to the model directly as a file
      (``attachments.build_agent_input``), so it sees the real layout and
      pixels; plain text is inlined; anything too large to attach falls back to
      the text extracted once on upload (``read_attachment`` gives the rest).
    * ``reference`` — an image the generators should follow: passed to
      ``generate_image`` as a reference, or to a video sequence to hold the look.
      Never sent to the text model itself.

    The bytes live in the assets dir (and blob storage when configured) like any
    other media; this row is the metadata plus the extracted-text fallback.
    """

    id: str = Field(default_factory=_uuid, primary_key=True)
    instruction_id: str = Field(index=True)
    filename: str = ""                       # as uploaded, for the agent to refer to
    content_type: str = ""
    purpose: AttachmentPurpose = AttachmentPurpose.context
    asset_path: str = ""                     # where the bytes are
    size_bytes: int = 0
    text: str = ""                           # extracted on upload; "" for images
    note: str = ""                           # what the human says this file is for
    created_at: datetime = Field(default_factory=_now)

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def label(self) -> str:
        return self.filename or self.id[:8]


class InstructionState(SQLModel, table=True):
    """Mutable per-instruction state that outlives a single run.

    Deliberately a SIDE TABLE rather than columns on ``Instruction``: SQLModel's
    ``create_all`` adds missing *tables* but never missing *columns*, so widening
    ``Instruction`` would break an existing database. A new table just appears.

    * ``memory`` — written by the AGENT through the ``memory`` tool: where it got
      to, what comes next, what it already covered. Injected into the next run's
      kickoff so a recurring instruction continues instead of repeating itself.
      Compacted by a summarizer when it grows past ``MEMORY_MAX_CHARS``.
    * ``note``   — written by the HUMAN in the dashboard: a standing correction
      ("prefer more recent sources") that steers subsequent runs without editing
      the brief. The agent never modifies it.
    """

    instruction_id: str = Field(primary_key=True)
    memory: str = ""
    note: str = ""
    memory_updated_at: datetime | None = None
    note_updated_at: datetime | None = None
    compactions: int = 0                     # how many times memory has been summarized


class Lock(SQLModel, table=True):
    """Single-flight lock: an atomic INSERT on ``key`` == acquiring the lock."""

    key: str = Field(primary_key=True)
    acquired_at: datetime = Field(default_factory=_now)
