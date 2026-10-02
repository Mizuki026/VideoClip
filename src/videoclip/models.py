from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionRecord(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        Index("ix_sessions_user_expiry", "user_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UploadHandoff(Base):
    __tablename__ = "upload_handoffs"
    __table_args__ = (
        Index("ix_upload_handoffs_state_created", "state", "created_at"),
        UniqueConstraint("writer_id", name="uq_upload_handoffs_writer"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    writer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    part_name: Mapped[str] = mapped_column(String(40), nullable=False)
    target_name: Mapped[str] = mapped_column(String(40), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    byte_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    media_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    duration_seconds: Mapped[float | None] = mapped_column()
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        UniqueConstraint("upload_id", name="uq_videos_upload_id"),
        Index("ix_videos_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    upload_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("upload_handoffs.id"))
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_name: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(nullable=False)
    media_info: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="ready", nullable=False)
    analysis_task_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_analysis_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "analysis_tasks.id",
            use_alter=True,
            name="fk_videos_current_analysis_task",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    deletion_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CleanupIntent(Base):
    __tablename__ = "cleanup_intents"
    __table_args__ = (
        Index("ix_cleanup_intents_created", "created_at"),
        UniqueConstraint("upload_id", "kind", name="uq_cleanup_intents_upload_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("upload_handoffs.id", ondelete="CASCADE")
    )
    storage_name: Mapped[str] = mapped_column(String(40), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"
    __table_args__ = (
        UniqueConstraint("video_id", name="uq_analysis_tasks_video"),
        CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_summary', 'failed')",
            name="ck_analysis_tasks_status",
        ),
        CheckConstraint(
            "stage IN ('queued', 'validating_source', 'extracting_audio', 'preparing_manifest', "
            "'transcribing', 'awaiting_summary', 'failed')",
            name="ck_analysis_tasks_stage",
        ),
        Index("ix_analysis_tasks_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="RESTRICT"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    current_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "analysis_attempts.id",
            use_alter=True,
            name="fk_analysis_tasks_current_attempt",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AnalysisAttempt(Base):
    __tablename__ = "analysis_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_analysis_attempts_task_number"),
        CheckConstraint("attempt_number > 0", name="ck_analysis_attempts_number_positive"),
        CheckConstraint("claim_count IN (0, 1)", name="ck_analysis_attempts_claim_once"),
        CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_summary', 'failed')",
            name="ck_analysis_attempts_status",
        ),
        CheckConstraint(
            "cleanup_state IN ('pending', 'cleaned', 'failed')",
            name="ck_analysis_attempts_cleanup_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_fingerprint: Mapped[str | None] = mapped_column(String(64))
    work_directory_name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    first_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    cleanup_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskDelivery(Base):
    __tablename__ = "task_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'unknown', 'failed')", name="ck_task_deliveries_status"
        ),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_attempts.id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    publish_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("attempt_id", "segment_number", name="uq_transcript_segments_number"),
        CheckConstraint("segment_number >= 0", name="ck_transcript_segments_number_nonnegative"),
        CheckConstraint("audio_start_seconds >= 0", name="ck_transcript_segments_audio_start"),
        CheckConstraint("audio_end_seconds > audio_start_seconds", name="ck_transcript_segments_audio_end"),
        CheckConstraint("video_start_seconds >= 0", name="ck_transcript_segments_video_start"),
        CheckConstraint("video_end_seconds > video_start_seconds", name="ck_transcript_segments_video_end"),
        CheckConstraint(
            "status IN ('expected', 'succeeded') AND "
            "((status = 'expected' AND text IS NULL) OR (status = 'succeeded' AND text IS NOT NULL))",
            name="ck_transcript_segments_status_text",
        ),
        Index("ix_transcript_segments_attempt_status", "attempt_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    segment_number: Mapped[int] = mapped_column(Integer, nullable=False)
    audio_start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    audio_end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    actual_audio_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    video_start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    video_end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    audio_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="expected")
    text: Mapped[str | None] = mapped_column(Text)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class AnalysisExecution(Base):
    __tablename__ = "analysis_executions"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_analysis_executions_attempt"),
        Index(
            "uq_analysis_executions_one_unconfirmed",
            text("(1)"),
            unique=True,
            postgresql_where=text("stop_confirmed_at IS NULL"),
        ),
        Index("ix_analysis_executions_started", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    backend_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stop_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_confirmation_source: Mapped[str | None] = mapped_column(String(64))
    stop_reason: Mapped[str | None] = mapped_column(String(64))
