"""add attempt lineage and checkpoint provenance

Revision ID: a81f06d2d712
Revises: 4c97a62f1a0b
Create Date: 2026-10-02 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a81f06d2d712"
down_revision: str | Sequence[str] | None = "4c97a62f1a0b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_attempts", sa.Column("parent_attempt_id", postgresql.UUID(as_uuid=True)))
    op.add_column("analysis_attempts", sa.Column("cleanup_error_code", sa.String(length=64)))
    op.create_foreign_key(
        "fk_analysis_attempts_parent",
        "analysis_attempts",
        "analysis_attempts",
        ["parent_attempt_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_analysis_attempts_parent",
        "analysis_attempts",
        ["parent_attempt_id"],
        unique=True,
        postgresql_where=sa.text("parent_attempt_id IS NOT NULL"),
    )
    op.add_column("transcript_segments", sa.Column("source_attempt_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_transcript_segments_source_attempt",
        "transcript_segments",
        "analysis_attempts",
        ["source_attempt_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_transcript_segments_source_attempt", "transcript_segments", type_="foreignkey")
    op.drop_column("transcript_segments", "source_attempt_id")
    op.drop_index("uq_analysis_attempts_parent", table_name="analysis_attempts")
    op.drop_constraint("fk_analysis_attempts_parent", "analysis_attempts", type_="foreignkey")
    op.drop_column("analysis_attempts", "cleanup_error_code")
    op.drop_column("analysis_attempts", "parent_attempt_id")
