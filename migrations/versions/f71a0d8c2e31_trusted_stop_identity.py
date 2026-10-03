"""bind analysis stops to supervised worker process boundaries

Revision ID: f71a0d8c2e31
Revises: e103a7c9f2d1
Create Date: 2026-10-03 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f71a0d8c2e31"
down_revision: str | Sequence[str] | None = "e103a7c9f2d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analysis_executions",
        sa.Column("worker_instance_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("analysis_executions", sa.Column("worker_container_id", sa.String(64), nullable=True))
    op.add_column("analysis_executions", sa.Column("process_scope", sa.String(32), nullable=True))
    op.add_column("analysis_executions", sa.Column("boundary_pid", sa.Integer(), nullable=True))
    op.add_column("analysis_executions", sa.Column("boundary_start_ticks", sa.BigInteger(), nullable=True))
    op.add_column("analysis_executions", sa.Column("boundary_session_id", sa.Integer(), nullable=True))
    op.add_column("analysis_executions", sa.Column("boundary_boot_id", sa.String(64), nullable=True))
    op.add_column(
        "analysis_executions",
        sa.Column("supervisor_registered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("analysis_executions", sa.Column("stop_evidence_sha256", sa.String(64), nullable=True))
    op.create_check_constraint(
        "ck_analysis_execution_boundary_binding",
        "analysis_executions",
        "(worker_instance_id IS NULL AND worker_container_id IS NULL AND process_scope IS NULL "
        "AND boundary_pid IS NULL AND boundary_start_ticks IS NULL AND boundary_session_id IS NULL "
        "AND boundary_boot_id IS NULL) OR (worker_instance_id IS NOT NULL "
        "AND worker_container_id IS NOT NULL AND process_scope IS NOT NULL AND boundary_pid IS NOT NULL "
        "AND boundary_start_ticks IS NOT NULL AND boundary_session_id IS NOT NULL "
        "AND boundary_boot_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_analysis_execution_boundary_binding", "analysis_executions", type_="check")
    op.drop_column("analysis_executions", "stop_evidence_sha256")
    op.drop_column("analysis_executions", "supervisor_registered_at")
    op.drop_column("analysis_executions", "boundary_boot_id")
    op.drop_column("analysis_executions", "boundary_session_id")
    op.drop_column("analysis_executions", "boundary_start_ticks")
    op.drop_column("analysis_executions", "boundary_pid")
    op.drop_column("analysis_executions", "process_scope")
    op.drop_column("analysis_executions", "worker_container_id")
    op.drop_column("analysis_executions", "worker_instance_id")
