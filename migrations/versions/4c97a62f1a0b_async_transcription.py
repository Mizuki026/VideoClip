"""add persisted analysis tasks, deliveries, executions, and transcript checkpoints

Revision ID: 4c97a62f1a0b
Revises: 482593f22c3e
Create Date: 2026-10-02 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4c97a62f1a0b"
down_revision: str | Sequence[str] | None = "482593f22c3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_summary', 'failed')", name="ck_analysis_tasks_status"
        ),
        sa.CheckConstraint(
            "stage IN ('queued', 'validating_source', 'extracting_audio', 'preparing_manifest', "
            "'transcribing', 'awaiting_summary', 'failed')",
            name="ck_analysis_tasks_stage",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("video_id", name="uq_analysis_tasks_video"),
    )
    op.create_index("ix_analysis_tasks_owner_created", "analysis_tasks", ["owner_id", "created_at"])

    op.create_table(
        "analysis_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("manifest_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("work_directory_name", sa.String(length=32), nullable=False),
        sa.Column("claim_count", sa.Integer(), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("first_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("cleanup_state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_number > 0", name="ck_analysis_attempts_number_positive"),
        sa.CheckConstraint("claim_count IN (0, 1)", name="ck_analysis_attempts_claim_once"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_summary', 'failed')",
            name="ck_analysis_attempts_status",
        ),
        sa.CheckConstraint(
            "cleanup_state IN ('pending', 'cleaned', 'failed')", name="ck_analysis_attempts_cleanup_state"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", name="uq_analysis_attempts_execution_id"),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_analysis_attempts_task_number"),
        sa.UniqueConstraint("work_directory_name", name="uq_analysis_attempts_work_directory"),
    )
    op.create_foreign_key(
        "fk_analysis_tasks_current_attempt",
        "analysis_tasks",
        "analysis_attempts",
        ["current_attempt_id"],
        ["id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "task_deliveries",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("publish_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'unknown', 'failed')", name="ck_task_deliveries_status"
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["analysis_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("attempt_id"),
    )

    op.create_table(
        "transcript_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_number", sa.Integer(), nullable=False),
        sa.Column("audio_start_seconds", sa.Float(), nullable=False),
        sa.Column("audio_end_seconds", sa.Float(), nullable=False),
        sa.Column("actual_audio_seconds", sa.Float(), nullable=False),
        sa.Column("video_start_seconds", sa.Float(), nullable=False),
        sa.Column("video_end_seconds", sa.Float(), nullable=False),
        sa.Column("audio_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("request_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("segment_number >= 0", name="ck_transcript_segments_number_nonnegative"),
        sa.CheckConstraint("audio_start_seconds >= 0", name="ck_transcript_segments_audio_start"),
        sa.CheckConstraint(
            "audio_end_seconds > audio_start_seconds", name="ck_transcript_segments_audio_end"
        ),
        sa.CheckConstraint("video_start_seconds >= 0", name="ck_transcript_segments_video_start"),
        sa.CheckConstraint(
            "video_end_seconds > video_start_seconds", name="ck_transcript_segments_video_end"
        ),
        sa.CheckConstraint(
            "status IN ('expected', 'succeeded') AND "
            "((status = 'expected' AND text IS NULL) OR (status = 'succeeded' AND text IS NOT NULL))",
            name="ck_transcript_segments_status_text",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["analysis_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "segment_number", name="uq_transcript_segments_number"),
    )
    op.create_index("ix_transcript_segments_attempt_status", "transcript_segments", ["attempt_id", "status"])

    op.create_table(
        "analysis_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("backend_pid", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("stop_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_confirmation_source", sa.String(length=64), nullable=True),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attempt_id"], ["analysis_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", name="uq_analysis_executions_attempt"),
    )
    op.create_index("ix_analysis_executions_started", "analysis_executions", ["started_at"])
    op.execute(
        "CREATE UNIQUE INDEX uq_analysis_executions_one_unconfirmed "
        "ON analysis_executions ((1)) WHERE stop_confirmed_at IS NULL"
    )

    op.add_column(
        "videos", sa.Column("current_analysis_task_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_videos_current_analysis_task",
        "videos",
        "analysis_tasks",
        ["current_analysis_task_id"],
        ["id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.execute(
        """
        CREATE FUNCTION prevent_analysis_config_mutation() RETURNS trigger AS $$
        BEGIN
            IF NEW.config_snapshot IS DISTINCT FROM OLD.config_snapshot
               OR NEW.config_sha256 IS DISTINCT FROM OLD.config_sha256
               OR NEW.source_fingerprint IS DISTINCT FROM OLD.source_fingerprint THEN
                RAISE EXCEPTION 'analysis attempt configuration is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_analysis_config_immutable BEFORE UPDATE ON analysis_attempts "
        "FOR EACH ROW EXECUTE FUNCTION prevent_analysis_config_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_analysis_config_immutable ON analysis_attempts")
    op.execute("DROP FUNCTION IF EXISTS prevent_analysis_config_mutation()")
    op.drop_constraint("fk_videos_current_analysis_task", "videos", type_="foreignkey")
    op.drop_column("videos", "current_analysis_task_id")
    op.execute("DROP INDEX IF EXISTS uq_analysis_executions_one_unconfirmed")
    op.drop_index("ix_analysis_executions_started", table_name="analysis_executions")
    op.drop_table("analysis_executions")
    op.drop_index("ix_transcript_segments_attempt_status", table_name="transcript_segments")
    op.drop_table("transcript_segments")
    op.drop_table("task_deliveries")
    op.drop_constraint("fk_analysis_tasks_current_attempt", "analysis_tasks", type_="foreignkey")
    op.drop_table("analysis_attempts")
    op.drop_index("ix_analysis_tasks_owner_created", table_name="analysis_tasks")
    op.drop_table("analysis_tasks")
