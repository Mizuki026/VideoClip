"""add durable summary execution and publication"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c52d4b5ad389"
down_revision: str | Sequence[str] | None = "a81f06d2d712"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_analysis_tasks_status", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_status",
        "analysis_tasks",
        "status IN ('queued', 'running', 'awaiting_summary', 'failed', 'succeeded')",
    )
    op.drop_constraint("ck_analysis_tasks_stage", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_stage",
        "analysis_tasks",
        "stage IN ('queued', 'validating_source', 'extracting_audio', 'preparing_manifest', "
        "'transcribing', 'awaiting_summary', 'queued_summary', 'summarizing', 'failed', 'succeeded')",
    )
    op.add_column(
        "analysis_tasks",
        sa.Column("summary_status", sa.String(length=24), nullable=False, server_default="not_started"),
    )
    op.execute("UPDATE analysis_tasks SET summary_status = 'waiting' WHERE status = 'awaiting_summary'")
    op.create_check_constraint(
        "ck_analysis_tasks_summary_status",
        "analysis_tasks",
        "summary_status IN ('not_started', 'waiting', 'running', 'failed', 'succeeded')",
    )

    op.drop_constraint("ck_analysis_attempts_status", "analysis_attempts", type_="check")
    op.create_check_constraint(
        "ck_analysis_attempts_status",
        "analysis_attempts",
        "status IN ('queued', 'running', 'awaiting_summary', 'failed', 'succeeded')",
    )
    op.add_column(
        "analysis_attempts",
        sa.Column("execution_mode", sa.String(length=24), nullable=False, server_default="full"),
    )
    op.create_check_constraint(
        "ck_analysis_attempts_execution_mode",
        "analysis_attempts",
        "execution_mode IN ('full', 'summary_only')",
    )

    op.create_table(
        "analysis_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("transcript_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("structure_version", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("response_schema_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["analysis_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_analysis_summaries_task"),
        sa.UniqueConstraint("video_id", name="uq_analysis_summaries_video"),
    )

    op.create_table(
        "summary_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_number", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("elapsed_seconds", sa.Float(), nullable=True),
        sa.Column("finish_reason", sa.String(length=64), nullable=True),
        sa.Column("provider_trace_id", sa.String(length=128), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_bytes", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("validation_error_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["analysis_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["execution_id"], ["analysis_executions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["analysis_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "request_number", name="uq_summary_requests_attempt_number"),
        sa.CheckConstraint("request_number BETWEEN 1 AND 3", name="ck_summary_requests_request_number"),
        sa.CheckConstraint(
            "status IN ('started', 'response_received', 'failed', 'succeeded')",
            name="ck_summary_requests_status",
        ),
    )
    op.create_index("ix_summary_requests_task_attempt", "summary_requests", ["task_id", "attempt_id"])


def downgrade() -> None:
    op.drop_index("ix_summary_requests_task_attempt", table_name="summary_requests")
    op.drop_table("summary_requests")
    op.drop_table("analysis_summaries")
    op.execute(
        "UPDATE analysis_attempts SET status = 'failed', failure_code = "
        "COALESCE(failure_code, 'SUMMARY_STAGE_DOWNGRADED') WHERE status = 'succeeded'"
    )
    op.execute(
        "UPDATE analysis_tasks SET status = 'failed', stage = 'failed', "
        "summary_status = 'failed', failure_code = COALESCE(failure_code, 'SUMMARY_STAGE_DOWNGRADED') "
        "WHERE status = 'succeeded'"
    )
    op.drop_constraint("ck_analysis_attempts_execution_mode", "analysis_attempts", type_="check")
    op.drop_column("analysis_attempts", "execution_mode")
    op.drop_constraint("ck_analysis_attempts_status", "analysis_attempts", type_="check")
    op.create_check_constraint(
        "ck_analysis_attempts_status",
        "analysis_attempts",
        "status IN ('queued', 'running', 'awaiting_summary', 'failed')",
    )
    op.drop_constraint("ck_analysis_tasks_summary_status", "analysis_tasks", type_="check")
    op.drop_column("analysis_tasks", "summary_status")
    op.drop_constraint("ck_analysis_tasks_stage", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_stage",
        "analysis_tasks",
        "stage IN ('queued', 'validating_source', 'extracting_audio', 'preparing_manifest', "
        "'transcribing', 'awaiting_summary', 'failed')",
    )
    op.drop_constraint("ck_analysis_tasks_status", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_status",
        "analysis_tasks",
        "status IN ('queued', 'running', 'awaiting_summary', 'failed')",
    )
