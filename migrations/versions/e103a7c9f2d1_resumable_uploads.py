"""resumable upload sessions

Revision ID: e103a7c9f2d1
Revises: c52d4b5ad389
Create Date: 2026-10-03 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e103a7c9f2d1"
down_revision: str | Sequence[str] | None = "c52d4b5ad389"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "upload_handoffs",
        sa.Column(
            "protocol_type",
            sa.String(length=16),
            server_default=sa.text("'ordinary'"),
            nullable=False,
        ),
    )
    op.add_column("upload_handoffs", sa.Column("declared_total_bytes", sa.BigInteger(), nullable=True))
    op.add_column(
        "upload_handoffs",
        sa.Column("expected_content_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "upload_handoffs",
        sa.Column("confirmed_offset", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("upload_handoffs", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("upload_handoffs", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.create_unique_constraint(
        "uq_upload_handoffs_owner_idempotency", "upload_handoffs", ["owner_id", "idempotency_key"]
    )
    op.create_index(
        "ix_upload_handoffs_protocol_expiry",
        "upload_handoffs",
        ["protocol_type", "expires_at", "state"],
        unique=False,
    )
    op.create_table(
        "upload_chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("upload_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("byte_offset", sa.BigInteger(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name="ck_upload_chunks_index_nonnegative"),
        sa.CheckConstraint("byte_offset >= 0", name="ck_upload_chunks_offset_nonnegative"),
        sa.CheckConstraint("byte_count > 0", name="ck_upload_chunks_length_positive"),
        sa.ForeignKeyConstraint(["upload_id"], ["upload_handoffs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("upload_id", "chunk_index", name="uq_upload_chunks_upload_index"),
    )
    op.create_index("ix_upload_chunks_upload_offset", "upload_chunks", ["upload_id", "byte_offset"])


def downgrade() -> None:
    bind = op.get_bind()
    pending = bind.execute(
        sa.text(
            "SELECT count(*) FROM upload_handoffs "
            "WHERE protocol_type = 'resumable' AND state <> 'complete'"
        )
    ).scalar_one()
    if pending:
        raise RuntimeError("cannot downgrade while resumable upload sessions are unfinished")
    op.drop_index("ix_upload_chunks_upload_offset", table_name="upload_chunks")
    op.drop_table("upload_chunks")
    op.drop_index("ix_upload_handoffs_protocol_expiry", table_name="upload_handoffs")
    op.drop_constraint("uq_upload_handoffs_owner_idempotency", "upload_handoffs", type_="unique")
    op.drop_column("upload_handoffs", "idempotency_key")
    op.drop_column("upload_handoffs", "expires_at")
    op.drop_column("upload_handoffs", "confirmed_offset")
    op.drop_column("upload_handoffs", "expected_content_sha256")
    op.drop_column("upload_handoffs", "declared_total_bytes")
    op.drop_column("upload_handoffs", "protocol_type")
