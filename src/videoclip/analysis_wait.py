from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from videoclip.model_cooling import ModelIdentity, require_utc
from videoclip.models import AnalysisAttempt, AnalysisTask


def wait_budget_error(
    *,
    task: AnalysisTask,
    now: datetime,
    cooldown_until: datetime,
    single_limit_seconds: int,
    total_limit_seconds: int,
) -> str | None:
    timestamp = require_utc(now)
    until = require_utc(cooldown_until)
    start = require_utc(task.model_wait_started_at) if task.model_wait_started_at else timestamp
    deadline = (
        require_utc(task.model_wait_single_deadline_at)
        if task.model_wait_single_deadline_at
        else start + timedelta(seconds=single_limit_seconds)
    )
    if until > deadline:
        return "MODEL_WAIT_SINGLE_LIMIT_EXCEEDED"
    previous = task.model_wait_total_seconds or 0
    active_elapsed = 0
    if task.model_wait_started_at is not None:
        active_elapsed = max(
            0,
            math.ceil((timestamp - require_utc(task.model_wait_started_at)).total_seconds()),
        )
    projected = previous + active_elapsed + max(0, math.ceil((until - timestamp).total_seconds()))
    if projected > total_limit_seconds:
        return "MODEL_WAIT_TOTAL_LIMIT_EXCEEDED"
    return None


def enter_model_wait(
    session: Session,
    task: AnalysisTask,
    attempt: AnalysisAttempt,
    *,
    identity: ModelIdentity,
    stage: str,
    origin: str,
    cooldown_until: datetime,
    now: datetime,
    single_limit_seconds: int,
    total_limit_seconds: int,
) -> None:
    timestamp = require_utc(now)
    until = require_utc(cooldown_until)
    started_at = require_utc(task.model_wait_started_at) if task.model_wait_started_at else timestamp
    task.model_wait_started_at = started_at
    task.model_wait_single_deadline_at = (
        require_utc(task.model_wait_single_deadline_at)
        if task.model_wait_single_deadline_at
        else started_at + timedelta(seconds=single_limit_seconds)
    )
    task.model_wait_until = until
    task.model_wait_reason = "MODEL_COOLDOWN"
    task.model_wait_error_code = None
    task.model_wait_provider = identity.provider
    task.model_wait_account_reference = identity.account_reference
    task.model_wait_model = identity.model
    task.model_wait_stage = stage
    task.model_wait_origin = origin
    task.delivery_next_check_at = until
    task.delivery_wait_reason = "shared_model_cooldown"
    task.queue_entered_at = None
    if task.delivery_deadline_at is not None and task.delivery_blocked_since is None:
        task.delivery_blocked_since = timestamp
    task.status = "waiting_model"
    task.stage = "waiting_asr" if stage == "transcribing" else "waiting_summary"
    task.failure_code = None
    if stage == "summarizing":
        task.summary_status = "waiting"
    attempt.status = "waiting_model"
    # Existing running work may encounter Stage-5 preflight after deployment.
    # Failed/awaiting-summary history is never enrolled by migration or scanning.
    if task.stage5_enrolled_at is None:
        task.stage5_enrolled_at = timestamp
        task.auto_recovery_count = 0
        task.auto_recovery_limit = 2
        task.model_wait_total_seconds = task.model_wait_total_seconds or 0
        task.model_wait_total_limit_seconds = total_limit_seconds


def require_manual_wait(task: AnalysisTask, attempt: AnalysisAttempt, error_code: str) -> None:
    task.status = "failed"
    task.stage = "failed"
    task.failure_code = error_code
    task.model_wait_reason = "MODEL_COOLDOWN"
    task.model_wait_error_code = error_code
    task.delivery_wait_reason = "model_wait_manual_required"
    task.delivery_next_check_at = None
    attempt.status = "failed"
    attempt.failure_code = attempt.failure_code or error_code


def finish_model_wait(task: AnalysisTask, *, now: datetime) -> None:
    timestamp = require_utc(now)
    if task.model_wait_started_at is not None:
        started = require_utc(task.model_wait_started_at)
        elapsed = max(0, math.ceil((timestamp - started).total_seconds()))
        task.model_wait_total_seconds = (task.model_wait_total_seconds or 0) + elapsed
    task.model_wait_reason = None
    task.model_wait_error_code = None
    task.model_wait_provider = None
    task.model_wait_account_reference = None
    task.model_wait_model = None
    task.model_wait_stage = None
    task.model_wait_origin = None
    task.model_wait_started_at = None
    task.model_wait_until = None
    task.model_wait_single_deadline_at = None
