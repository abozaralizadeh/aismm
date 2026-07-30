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


class RunStatus(str, enum.Enum):
    running = "running"
    published = "published"
    staged = "staged"        # dry-run preview or queued for approval
    skipped = "skipped"      # e.g. locked by a concurrent run
    failed = "failed"


class StagedStatus(str, enum.Enum):
    preview = "preview"                    # dry-run: informational only
    pending_approval = "pending_approval"  # waiting for a dashboard click
    approved = "approved"
    rejected = "rejected"
    published = "published"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
class Account(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
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
    name: str
    brief: str = ""                          # the persona / directive / theme text
    account_ids_json: str = "[]"             # selected accounts (multi-select)
    schedule: str = ""                       # cron ("0 9 * * *") or interval ("every 6h")
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


class Run(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    instruction_id: str = Field(index=True)
    account_id: str = Field(index=True)
    status: RunStatus = RunStatus.running
    caption: str = ""
    asset_path: str = ""
    external_url: str = ""                    # published permalink, when live
    error: str = ""
    log: str = ""                            # short human-readable trace of the run
    # The kickoff prompt this run actually received — brief + memory + note +
    # platform rules, as composed at the time. Kept so a failed run can be
    # debugged from what the agent was told, not from what the instruction says now.
    prompt: str = ""
    created_at: datetime = Field(default_factory=_now)


class StagedPost(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    instruction_id: str = Field(index=True)
    account_id: str = Field(index=True)
    run_id: str = ""
    caption: str = ""
    asset_path: str = ""
    media_kind: str = "text"                 # text | image | video
    # A carousel has several files; asset_path keeps the first for previews.
    asset_paths_json: str = "[]"
    placement: str = "feed"                  # feed | story | reel
    status: StagedStatus = StagedStatus.preview
    external_url: str = ""
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
