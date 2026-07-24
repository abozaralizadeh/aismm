"""Persistence models + enums (SQLModel).

Four entities drive the whole system:

* ``Account``      — a connected social profile (platform + encrypted OAuth tokens).
* ``Instruction``  — a dashboard-authored directive: a brief + selected accounts +
                     schedule + publish mode. This is what the scheduler fires.
* ``Run``          — one execution of an instruction against one account.
* ``StagedPost``   — a prepared-but-not-yet-live post (dry-run preview or an
                     approval-queue item awaiting a human click in the dashboard).

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
    created_at: datetime = Field(default_factory=_now)


class StagedPost(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    instruction_id: str = Field(index=True)
    account_id: str = Field(index=True)
    run_id: str = ""
    caption: str = ""
    asset_path: str = ""
    media_kind: str = "text"                 # text | image | video
    status: StagedStatus = StagedStatus.preview
    external_url: str = ""
    created_at: datetime = Field(default_factory=_now)


class Lock(SQLModel, table=True):
    """Single-flight lock: an atomic INSERT on ``key`` == acquiring the lock."""

    key: str = Field(primary_key=True)
    acquired_at: datetime = Field(default_factory=_now)
