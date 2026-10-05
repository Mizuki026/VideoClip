"""Add independent original-media lifecycle and pinned note attempt."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "d42f6c8a901e"
down_revision = "7c2a91e5b04d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    unknown_status_count = int(
        connection.execute(
            text("SELECT count(*) FROM videos WHERE status NOT IN ('ready', 'deleting', 'cleanup_failed')")
        ).scalar_one()
    )
    if unknown_status_count:
        raise RuntimeError(
            "videos contains an unknown whole-record status; inspect before media-state migration"
        )

    op.add_column("videos", sa.Column("media_status", sa.String(length=24), nullable=True))
    op.add_column("videos", sa.Column("media_error_code", sa.String(length=64), nullable=True))
    op.add_column("analysis_tasks", sa.Column("notes_attempt_id", sa.Uuid(), nullable=True))

    op.execute(
        text(
            "UPDATE videos AS video SET "
            "media_status = CASE "
            "WHEN video.status = 'ready' AND (video.deletion_error IS NOT NULL OR EXISTS ("
            "SELECT 1 FROM cleanup_intents intent WHERE intent.upload_id = video.upload_id "
            "AND intent.kind = 'video')) THEN 'cleanup_failed' "
            "WHEN video.status = 'ready' THEN 'available' "
            "WHEN video.status = 'deleting' THEN 'deletion_pending' "
            "ELSE 'cleanup_failed' END, "
            "media_error_code = CASE "
            "WHEN video.status = 'ready' AND video.deletion_error IS NOT NULL THEN video.deletion_error "
            "WHEN video.status = 'ready' AND EXISTS (SELECT 1 FROM cleanup_intents intent "
            "WHERE intent.upload_id = video.upload_id AND intent.kind = 'video') "
            "THEN 'LEGACY_CLEANUP_INTENT_PRESENT' "
            "WHEN video.status = 'cleanup_failed' THEN COALESCE(video.deletion_error, "
            "'LEGACY_CLEANUP_FAILED') "
            "ELSE NULL END"
        )
    )
    op.alter_column(
        "videos",
        "media_status",
        existing_type=sa.String(length=24),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_videos_media_status",
        "videos",
        "media_status IN ('available', 'deletion_pending', 'cleanup_failed', 'deleted')",
    )
    op.create_foreign_key(
        "fk_analysis_tasks_notes_attempt",
        "analysis_tasks",
        "analysis_attempts",
        ["notes_attempt_id"],
        ["id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint("fk_analysis_tasks_notes_attempt", "analysis_tasks", type_="foreignkey")
    op.drop_column("analysis_tasks", "notes_attempt_id")
    op.drop_constraint("ck_videos_media_status", "videos", type_="check")
    op.drop_column("videos", "media_error_code")
    op.drop_column("videos", "media_status")
