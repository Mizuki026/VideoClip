from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from videoclip.models import AnalysisEvent, AnalysisTask

# 时间线事件名采用白名单，事件内容只保存内部引用，不写入转写正文或模型响应。
EVENT_TYPES = frozenset(
    {
        "analysis_submitted",
        "delivery_reserved",
        "delivery_sent",
        "delivery_retry_scheduled",
        "execution_claimed",
        "execution_failed",
        "checkpoint_saved",
        "transcription_completed",
        "summary_requested",
        "summary_response_received",
        "summary_failed",
        "summary_published",
        "model_wait_entered",
        "model_wait_resumed",
        "model_rate_limited",
        "model_overloaded",
        "automatic_recovery_created",
        "manual_recovery_created",
        "manual_continuation_created",
        "stop_confirmed",
        "attempt_cleanup_started",
        "attempt_cleanup_completed",
        "attempt_cleanup_failed",
        "media_cleanup_started",
        "media_cleanup_completed",
        "media_cleanup_failed",
        "record_cleanup_started",
        "record_cleanup_failed",
    }
)
_SAFE_REQUEST_REFERENCE = re.compile(
    r"^(?:summary-request:[0-9a-fA-F-]{36}|transcript-segment:[0-9a-fA-F-]{36}:[0-9]{1,5})$"
)


def record_analysis_event(
    session: Session,
    *,
    task_id: uuid.UUID,
    event_key: str,
    event_type: str,
    attempt_id: uuid.UUID | None = None,
    execution_id: uuid.UUID | None = None,
    request_reference: str | None = None,
    stage: str | None = None,
    error_code: str | None = None,
    actor: str = "system",
) -> AnalysisEvent | None:
    """Append a safe event in the owning transaction, serialized by the task row lock."""
    if event_type not in EVENT_TYPES:
        raise ValueError("unsupported analysis event type")
    if not event_key or len(event_key) > 180:
        raise ValueError("analysis event key must contain 1 to 180 characters")
    if actor not in {"system", "user"}:
        raise ValueError("unsupported analysis event actor")
    if request_reference is not None and not _SAFE_REQUEST_REFERENCE.fullmatch(request_reference):
        raise ValueError("analysis event request reference must be a safe internal identifier")
    task = session.scalar(select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update())
    if task is None:
        return None
    existing = session.scalar(
        select(AnalysisEvent).where(
            AnalysisEvent.task_id == task.id,
            AnalysisEvent.event_key == event_key,
        )
    )
    if existing is not None:
        return existing
    event = AnalysisEvent(
        owner_id=task.owner_id,
        video_id=task.video_id,
        task_id=task.id,
        attempt_id=attempt_id,
        execution_id=execution_id,
        request_reference=request_reference,
        event_key=event_key,
        event_type=event_type,
        stage=stage,
        error_code=error_code[:64] if error_code else None,
        actor=actor,
    )
    session.add(event)
    session.flush()
    return event
