from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from videoclip.models import AnalysisAttempt, AnalysisExecution, AnalysisTask, Video

EXECUTION_ADVISORY_LOCK_KEY = 0x566964656F436C69


def try_acquire_execution_gate(session: Session) -> bool:
    """Serialize a short delivery reservation with the Worker execution lease."""
    return bool(
        session.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": EXECUTION_ADVISORY_LOCK_KEY},
        ).scalar_one()
    )


def find_unconfirmed_execution(session: Session) -> AnalysisExecution | None:
    """Return the deployment-wide execution that still holds the analysis slot."""
    return session.scalar(
        select(AnalysisExecution)
        .where(AnalysisExecution.stop_confirmed_at.is_(None))
        .order_by(AnalysisExecution.started_at, AnalysisExecution.id)
        .limit(1)
    )


def find_next_queued_task(session: Session) -> uuid.UUID | None:
    """Return the oldest currently eligible Stage-4 task by its durable queue key."""
    return session.scalar(
        select(AnalysisTask.id)
        .join(AnalysisAttempt, AnalysisAttempt.id == AnalysisTask.current_attempt_id)
        .join(Video, Video.id == AnalysisTask.video_id)
        .where(
            AnalysisTask.status == "queued",
            AnalysisTask.queue_entered_at.is_not(None),
            AnalysisAttempt.status == "queued",
            AnalysisAttempt.claim_count == 0,
            AnalysisAttempt.execution_id.is_(None),
            Video.status == "ready",
            Video.current_analysis_task_id == AnalysisTask.id,
            ~select(AnalysisExecution.id)
            .where(AnalysisExecution.attempt_id == AnalysisAttempt.id)
            .exists(),
        )
        .order_by(AnalysisTask.queue_entered_at, AnalysisTask.id)
        .limit(1)
    )
