"""add shared model cooldown and bounded automatic recovery"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c2a91e5b04d"
down_revision: str | Sequence[str] | None = "b4c91e7a2d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_analysis_tasks_status", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_status",
        "analysis_tasks",
        "status IN ('queued', 'running', 'waiting_model', 'awaiting_summary', 'failed', 'succeeded')",
    )
    op.drop_constraint("ck_analysis_tasks_stage", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_stage",
        "analysis_tasks",
        "stage IN ('queued', 'validating_source', 'extracting_audio', 'preparing_manifest', "
        "'transcribing', 'waiting_asr', 'waiting_summary', 'awaiting_summary', 'queued_summary', "
        "'summarizing', 'failed', 'succeeded')",
    )
    op.drop_constraint("ck_analysis_attempts_status", "analysis_attempts", type_="check")
    op.create_check_constraint(
        "ck_analysis_attempts_status",
        "analysis_attempts",
        "status IN ('queued', 'running', 'waiting_model', 'awaiting_summary', 'failed', 'succeeded')",
    )

    op.add_column("analysis_tasks", sa.Column("auto_recovery_count", sa.Integer()))
    op.add_column("analysis_tasks", sa.Column("auto_recovery_limit", sa.Integer()))
    op.add_column("analysis_tasks", sa.Column("stage5_enrolled_at", sa.DateTime(timezone=True)))
    op.add_column("analysis_tasks", sa.Column("model_wait_total_seconds", sa.Integer()))
    op.add_column("analysis_tasks", sa.Column("model_wait_total_limit_seconds", sa.Integer()))
    op.add_column("analysis_tasks", sa.Column("model_wait_reason", sa.String(length=64)))
    op.add_column("analysis_tasks", sa.Column("model_wait_error_code", sa.String(length=64)))
    op.add_column("analysis_tasks", sa.Column("model_wait_provider", sa.String(length=32)))
    op.add_column("analysis_tasks", sa.Column("model_wait_account_reference", sa.String(length=64)))
    op.add_column("analysis_tasks", sa.Column("model_wait_model", sa.String(length=128)))
    op.add_column("analysis_tasks", sa.Column("model_wait_stage", sa.String(length=24)))
    op.add_column("analysis_tasks", sa.Column("model_wait_origin", sa.String(length=32)))
    op.add_column("analysis_tasks", sa.Column("model_wait_started_at", sa.DateTime(timezone=True)))
    op.add_column("analysis_tasks", sa.Column("model_wait_until", sa.DateTime(timezone=True)))
    op.add_column("analysis_tasks", sa.Column("model_wait_single_deadline_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_analysis_tasks_auto_recovery_count_nonnegative",
        "analysis_tasks",
        "auto_recovery_count IS NULL OR auto_recovery_count >= 0",
    )
    op.create_check_constraint(
        "ck_analysis_tasks_auto_recovery_limit_positive",
        "analysis_tasks",
        "auto_recovery_limit IS NULL OR auto_recovery_limit > 0",
    )
    op.create_check_constraint(
        "ck_analysis_tasks_auto_recoveries_within_limit",
        "analysis_tasks",
        "auto_recovery_count IS NULL OR auto_recovery_limit IS NULL "
        "OR auto_recovery_count <= auto_recovery_limit",
    )
    op.create_check_constraint(
        "ck_analysis_tasks_model_wait_total_nonnegative",
        "analysis_tasks",
        "model_wait_total_seconds IS NULL OR model_wait_total_seconds >= 0",
    )
    op.create_check_constraint(
        "ck_analysis_tasks_model_wait_total_limit_positive",
        "analysis_tasks",
        "model_wait_total_limit_seconds IS NULL OR model_wait_total_limit_seconds > 0",
    )
    op.create_index(
        "ix_analysis_tasks_model_wait_due",
        "analysis_tasks",
        ["delivery_next_check_at", "id"],
        postgresql_where=sa.text("status = 'waiting_model' AND delivery_next_check_at IS NOT NULL"),
    )

    op.create_table(
        "shared_model_cooldowns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("account_reference", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consecutive_rate_limits", sa.Integer(), nullable=False),
        sa.Column("last_rate_limited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("consecutive_rate_limits >= 0", name="ck_shared_model_cooldown_count_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "account_reference", "model", name="uq_shared_model_cooldown_identity"
        ),
    )
    op.create_index(
        "ix_shared_model_cooldowns_until",
        "shared_model_cooldowns",
        ["cooldown_until"],
    )
    op.add_column("summary_requests", sa.Column("correction_error_code", sa.String(length=64)))


def downgrade() -> None:
    op.drop_column("summary_requests", "correction_error_code")
    op.drop_index("ix_shared_model_cooldowns_until", table_name="shared_model_cooldowns")
    op.drop_table("shared_model_cooldowns")
    op.drop_index("ix_analysis_tasks_model_wait_due", table_name="analysis_tasks")
    op.drop_constraint("ck_analysis_tasks_model_wait_total_nonnegative", "analysis_tasks", type_="check")
    op.drop_constraint("ck_analysis_tasks_model_wait_total_limit_positive", "analysis_tasks", type_="check")
    op.drop_constraint("ck_analysis_tasks_auto_recoveries_within_limit", "analysis_tasks", type_="check")
    op.drop_constraint("ck_analysis_tasks_auto_recovery_limit_positive", "analysis_tasks", type_="check")
    op.drop_constraint("ck_analysis_tasks_auto_recovery_count_nonnegative", "analysis_tasks", type_="check")
    op.drop_column("analysis_tasks", "model_wait_single_deadline_at")
    op.drop_column("analysis_tasks", "model_wait_until")
    op.drop_column("analysis_tasks", "model_wait_started_at")
    op.drop_column("analysis_tasks", "model_wait_origin")
    op.drop_column("analysis_tasks", "model_wait_stage")
    op.drop_column("analysis_tasks", "model_wait_model")
    op.drop_column("analysis_tasks", "model_wait_account_reference")
    op.drop_column("analysis_tasks", "model_wait_provider")
    op.drop_column("analysis_tasks", "model_wait_error_code")
    op.drop_column("analysis_tasks", "model_wait_reason")
    op.drop_column("analysis_tasks", "model_wait_total_seconds")
    op.drop_column("analysis_tasks", "model_wait_total_limit_seconds")
    op.drop_column("analysis_tasks", "stage5_enrolled_at")
    op.drop_column("analysis_tasks", "auto_recovery_limit")
    op.drop_column("analysis_tasks", "auto_recovery_count")
    op.drop_constraint("ck_analysis_attempts_status", "analysis_attempts", type_="check")
    op.create_check_constraint(
        "ck_analysis_attempts_status",
        "analysis_attempts",
        "status IN ('queued', 'running', 'awaiting_summary', 'failed', 'succeeded')",
    )
    op.drop_constraint("ck_analysis_tasks_stage", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_stage",
        "analysis_tasks",
        "stage IN ('queued', 'validating_source', 'extracting_audio', 'preparing_manifest', "
        "'transcribing', 'awaiting_summary', 'queued_summary', 'summarizing', 'failed', 'succeeded')",
    )
    op.drop_constraint("ck_analysis_tasks_status", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_status",
        "analysis_tasks",
        "status IN ('queued', 'running', 'awaiting_summary', 'failed', 'succeeded')",
    )
