"""persist bounded analysis delivery coordination

Revision ID: b4c91e7a2d60
Revises: f71a0d8c2e31
Create Date: 2026-10-03 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c91e7a2d60"
down_revision: str | Sequence[str] | None = "f71a0d8c2e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_tasks", sa.Column("queue_entered_at", sa.DateTime(timezone=True)))
    op.add_column("analysis_tasks", sa.Column("delivery_next_check_at", sa.DateTime(timezone=True)))
    op.add_column("analysis_tasks", sa.Column("delivery_retry_count", sa.Integer()))
    op.add_column("analysis_tasks", sa.Column("delivery_retry_limit", sa.Integer()))
    op.add_column("analysis_tasks", sa.Column("delivery_operation_count", sa.Integer()))
    op.add_column("analysis_tasks", sa.Column("delivery_deadline_at", sa.DateTime(timezone=True)))
    op.add_column("analysis_tasks", sa.Column("delivery_blocked_since", sa.DateTime(timezone=True)))
    op.add_column("analysis_tasks", sa.Column("delivery_wait_reason", sa.String(length=64)))
    op.add_column("analysis_tasks", sa.Column("delivery_failure_code", sa.String(length=64)))
    op.create_check_constraint(
        "ck_analysis_tasks_delivery_retry_count_nonnegative",
        "analysis_tasks",
        "delivery_retry_count IS NULL OR delivery_retry_count >= 0",
    )
    op.create_check_constraint(
        "ck_analysis_tasks_delivery_retry_limit_positive",
        "analysis_tasks",
        "delivery_retry_limit IS NULL OR delivery_retry_limit > 0",
    )
    op.create_check_constraint(
        "ck_analysis_tasks_delivery_operation_count_nonnegative",
        "analysis_tasks",
        "delivery_operation_count IS NULL OR delivery_operation_count >= 0",
    )
    op.create_check_constraint(
        "ck_analysis_tasks_delivery_retries_within_limit",
        "analysis_tasks",
        "delivery_retry_count IS NULL OR delivery_retry_limit IS NULL "
        "OR delivery_retry_count <= delivery_retry_limit",
    )
    op.create_index(
        "ix_analysis_tasks_delivery_due",
        "analysis_tasks",
        ["queue_entered_at", "delivery_next_check_at", "id"],
        postgresql_where=sa.text("status = 'queued' AND queue_entered_at IS NOT NULL"),
    )
    op.add_column("task_deliveries", sa.Column("operation_generation", sa.Integer()))
    op.create_check_constraint(
        "ck_task_deliveries_operation_generation_nonnegative",
        "task_deliveries",
        "operation_generation IS NULL OR operation_generation >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_task_deliveries_operation_generation_nonnegative", "task_deliveries", type_="check"
    )
    op.drop_column("task_deliveries", "operation_generation")
    op.drop_index("ix_analysis_tasks_delivery_due", table_name="analysis_tasks")
    op.drop_constraint("ck_analysis_tasks_delivery_retries_within_limit", "analysis_tasks", type_="check")
    op.drop_constraint(
        "ck_analysis_tasks_delivery_operation_count_nonnegative", "analysis_tasks", type_="check"
    )
    op.drop_constraint("ck_analysis_tasks_delivery_retry_limit_positive", "analysis_tasks", type_="check")
    op.drop_constraint("ck_analysis_tasks_delivery_retry_count_nonnegative", "analysis_tasks", type_="check")
    op.drop_column("analysis_tasks", "delivery_failure_code")
    op.drop_column("analysis_tasks", "delivery_wait_reason")
    op.drop_column("analysis_tasks", "delivery_blocked_since")
    op.drop_column("analysis_tasks", "delivery_deadline_at")
    op.drop_column("analysis_tasks", "delivery_operation_count")
    op.drop_column("analysis_tasks", "delivery_retry_limit")
    op.drop_column("analysis_tasks", "delivery_retry_count")
    op.drop_column("analysis_tasks", "delivery_next_check_at")
    op.drop_column("analysis_tasks", "queue_entered_at")
