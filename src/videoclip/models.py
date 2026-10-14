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
        Index("ix_upload_handoffs_protocol_expiry", "protocol_type", "expires_at", "state"),
        UniqueConstraint("writer_id", name="uq_upload_handoffs_writer"),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_upload_handoffs_owner_idempotency"),
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
    protocol_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ordinary", server_default=text("'ordinary'")
    )
    declared_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    expected_content_sha256: Mapped[str | None] = mapped_column(String(64))
    confirmed_offset: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UploadChunk(Base):
    __tablename__ = "upload_chunks"
    __table_args__ = (
        UniqueConstraint("upload_id", "chunk_index", name="uq_upload_chunks_upload_index"),
        CheckConstraint("chunk_index >= 0", name="ck_upload_chunks_index_nonnegative"),
        CheckConstraint("byte_offset >= 0", name="ck_upload_chunks_offset_nonnegative"),
        CheckConstraint("byte_count > 0", name="ck_upload_chunks_length_positive"),
        Index("ix_upload_chunks_upload_offset", "upload_id", "byte_offset"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    upload_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("upload_handoffs.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    media_status: Mapped[str] = mapped_column(String(24), default="available", nullable=False)
    media_error_code: Mapped[str | None] = mapped_column(String(64))
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


# 任务行保存用户可见状态和当前尝试指针；单次执行身份由独立记录跟踪。
class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"
    __table_args__ = (
        UniqueConstraint("video_id", name="uq_analysis_tasks_video"),
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting_model', 'awaiting_summary', 'failed', 'succeeded')",
            name="ck_analysis_tasks_status",
        ),
        CheckConstraint(
            "stage IN ('queued', 'validating_source', 'extracting_audio', 'preparing_manifest', "
            "'transcribing', 'waiting_asr', 'waiting_summary', 'awaiting_summary', 'queued_summary', "
            "'summarizing', 'failed', 'succeeded')",
            name="ck_analysis_tasks_stage",
        ),
        CheckConstraint(
            "summary_status IN ('not_started', 'waiting', 'running', 'failed', 'succeeded')",
            name="ck_analysis_tasks_summary_status",
        ),
        Index("ix_analysis_tasks_owner_created", "owner_id", "created_at"),
        Index(
            "ix_analysis_tasks_delivery_due",
            "queue_entered_at",
            "delivery_next_check_at",
            "id",
            postgresql_where=text("status = 'queued' AND queue_entered_at IS NOT NULL"),
        ),
        Index(
            "ix_analysis_tasks_model_wait_due",
            "delivery_next_check_at",
            "id",
            postgresql_where=text("status = 'waiting_model' AND delivery_next_check_at IS NOT NULL"),
        ),
        CheckConstraint(
            "auto_recovery_count IS NULL OR auto_recovery_count >= 0",
            name="ck_analysis_tasks_auto_recovery_count_nonnegative",
        ),
        CheckConstraint(
            "auto_recovery_limit IS NULL OR auto_recovery_limit > 0",
            name="ck_analysis_tasks_auto_recovery_limit_positive",
        ),
        CheckConstraint(
            "auto_recovery_count IS NULL OR auto_recovery_limit IS NULL "
            "OR auto_recovery_count <= auto_recovery_limit",
            name="ck_analysis_tasks_auto_recoveries_within_limit",
        ),
        CheckConstraint(
            "model_wait_total_seconds IS NULL OR model_wait_total_seconds >= 0",
            name="ck_analysis_tasks_model_wait_total_nonnegative",
        ),
        CheckConstraint(
            "model_wait_total_limit_seconds IS NULL OR model_wait_total_limit_seconds > 0",
            name="ck_analysis_tasks_model_wait_total_limit_positive",
        ),
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
    notes_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "analysis_attempts.id",
            use_alter=True,
            name="fk_analysis_tasks_notes_attempt",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    summary_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_started")
    failure_code: Mapped[str | None] = mapped_column(String(64))
    # NULL fields deliberately keep pre-Stage-4 history outside automatic coordination.
    queue_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_retry_count: Mapped[int | None] = mapped_column(Integer)
    delivery_retry_limit: Mapped[int | None] = mapped_column(Integer)
    delivery_operation_count: Mapped[int | None] = mapped_column(Integer)
    delivery_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_blocked_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_wait_reason: Mapped[str | None] = mapped_column(String(64))
    delivery_failure_code: Mapped[str | None] = mapped_column(String(64))
    # Stage 5 is explicit: nullable legacy fields are not an automatic-recovery grant.
    auto_recovery_count: Mapped[int | None] = mapped_column(Integer)
    auto_recovery_limit: Mapped[int | None] = mapped_column(Integer)
    stage5_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_wait_total_seconds: Mapped[int | None] = mapped_column(Integer)
    model_wait_total_limit_seconds: Mapped[int | None] = mapped_column(Integer)
    model_wait_reason: Mapped[str | None] = mapped_column(String(64))
    model_wait_error_code: Mapped[str | None] = mapped_column(String(64))
    model_wait_provider: Mapped[str | None] = mapped_column(String(32))
    model_wait_account_reference: Mapped[str | None] = mapped_column(String(64))
    model_wait_model: Mapped[str | None] = mapped_column(String(128))
    model_wait_stage: Mapped[str | None] = mapped_column(String(24))
    model_wait_origin: Mapped[str | None] = mapped_column(String(32))
    model_wait_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_wait_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_wait_single_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
            "status IN ('queued', 'running', 'waiting_model', 'awaiting_summary', 'failed', 'succeeded')",
            name="ck_analysis_attempts_status",
        ),
        CheckConstraint(
            "execution_mode IN ('full', 'summary_only')", name="ck_analysis_attempts_execution_mode"
        ),
        CheckConstraint(
            "cleanup_state IN ('pending', 'cleaned', 'failed')",
            name="ck_analysis_attempts_cleanup_state",
        ),
        Index(
            "uq_analysis_attempts_parent",
            "parent_attempt_id",
            unique=True,
            postgresql_where=text("parent_attempt_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    parent_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_attempts.id", ondelete="RESTRICT")
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    execution_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="full")
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
    cleanup_error_code: Mapped[str | None] = mapped_column(String(64))
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
    operation_generation: Mapped[int | None] = mapped_column(Integer)
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
    source_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_attempts.id", ondelete="RESTRICT")
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


# 部分唯一索引约束全局未确认停止的执行记录最多只有一条。
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
        CheckConstraint(
            "(worker_instance_id IS NULL AND worker_container_id IS NULL AND process_scope IS NULL "
            "AND boundary_pid IS NULL AND boundary_start_ticks IS NULL AND boundary_session_id IS NULL "
            "AND boundary_boot_id IS NULL) OR (worker_instance_id IS NOT NULL "
            "AND worker_container_id IS NOT NULL AND process_scope IS NOT NULL AND boundary_pid IS NOT NULL "
            "AND boundary_start_ticks IS NOT NULL AND boundary_session_id IS NOT NULL "
            "AND boundary_boot_id IS NOT NULL)",
            name="ck_analysis_execution_boundary_binding",
        ),
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
    worker_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    worker_container_id: Mapped[str | None] = mapped_column(String(64))
    process_scope: Mapped[str | None] = mapped_column(String(32))
    boundary_pid: Mapped[int | None] = mapped_column(Integer)
    boundary_start_ticks: Mapped[int | None] = mapped_column(BigInteger)
    boundary_session_id: Mapped[int | None] = mapped_column(Integer)
    boundary_boot_id: Mapped[str | None] = mapped_column(String(64))
    supervisor_registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stop_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_confirmation_source: Mapped[str | None] = mapped_column(String(64))
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    stop_evidence_sha256: Mapped[str | None] = mapped_column(String(64))


class AnalysisSummary(Base):
    __tablename__ = "analysis_summaries"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_analysis_summaries_task"),
        UniqueConstraint("video_id", name="uq_analysis_summaries_video"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_tasks.id", ondelete="CASCADE"), nullable=False
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    manifest_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    transcript_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    structure_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_schema_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SummaryRequest(Base):
    __tablename__ = "summary_requests"
    __table_args__ = (
        UniqueConstraint("attempt_id", "request_number", name="uq_summary_requests_attempt_number"),
        CheckConstraint("request_number BETWEEN 1 AND 3", name="ck_summary_requests_request_number"),
        CheckConstraint(
            "status IN ('started', 'response_received', 'failed', 'succeeded')",
            name="ck_summary_requests_status",
        ),
        Index("ix_summary_requests_task_attempt", "task_id", "attempt_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_tasks.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_attempts.id", ondelete="CASCADE"), nullable=False
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_executions.id", ondelete="CASCADE"), nullable=False
    )
    request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="started")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)
    finish_reason: Mapped[str | None] = mapped_column(String(64))
    provider_trace_id: Mapped[str | None] = mapped_column(String(128))
    usage: Mapped[dict[str, int] | None] = mapped_column(JSONB)
    response_bytes: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    validation_error_code: Mapped[str | None] = mapped_column(String(64))
    correction_error_code: Mapped[str | None] = mapped_column(String(64))


class SharedModelCooldown(Base):
    __tablename__ = "shared_model_cooldowns"
    __table_args__ = (
        UniqueConstraint("provider", "account_reference", "model", name="uq_shared_model_cooldown_identity"),
        CheckConstraint("consecutive_rate_limits >= 0", name="ck_shared_model_cooldown_count_nonnegative"),
        Index("ix_shared_model_cooldowns_until", "cooldown_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    account_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    cooldown_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consecutive_rate_limits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_rate_limited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AnalysisEvent(Base):
    __tablename__ = "analysis_events"
    __table_args__ = (
        UniqueConstraint("task_id", "event_key", name="uq_analysis_events_task_key"),
        CheckConstraint("actor IN ('system', 'user')", name="ck_analysis_events_actor"),
        Index("ix_analysis_events_owner_video_id", "owner_id", "video_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_tasks.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    request_reference: Mapped[str | None] = mapped_column(String(128))
    event_key: Mapped[str] = mapped_column(String(180), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"), nullable=False
    )
