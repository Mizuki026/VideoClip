from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from kombu.exceptions import EncodeError  # type: ignore[import-untyped]
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from videoclip.events import record_analysis_event
from videoclip.execution_gate import (
    find_next_queued_task,
    find_unconfirmed_execution,
    try_acquire_execution_gate,
)
from videoclip.models import AnalysisAttempt, AnalysisExecution, AnalysisTask, TaskDelivery, Video
from videoclip.services import db_transaction
from videoclip.settings import Settings
from videoclip.stop_confirmation import PROCESS_SCOPE, identity_from_execution

logger = logging.getLogger("videoclip.delivery")

Publisher = Callable[[str, str], object]
DeliveryOutcome = Literal["sent", "unknown", "failed"]


@dataclass(frozen=True)
class DeliveryPolicy:
    retry_limit: int = 8
    deadline_seconds: int = 86_400
    scan_limit: int = 4
    interval_seconds: int = 60
    operation_timeout_seconds: int = 60
    backoff_base_seconds: int = 60
    backoff_max_seconds: int = 900

    @classmethod
    def from_settings(cls, settings: Settings) -> DeliveryPolicy:
        return cls(
            retry_limit=settings.analysis_delivery_retry_limit,
            deadline_seconds=settings.analysis_delivery_deadline_seconds,
            scan_limit=settings.analysis_delivery_scan_limit,
            interval_seconds=settings.analysis_delivery_interval_seconds,
            operation_timeout_seconds=settings.analysis_delivery_operation_timeout_seconds,
            backoff_base_seconds=settings.analysis_delivery_backoff_base_seconds,
            backoff_max_seconds=settings.analysis_delivery_backoff_max_seconds,
        )


@dataclass(frozen=True)
class DeliveryReservation:
    task_id: uuid.UUID
    attempt_id: uuid.UUID
    generation: int
    retry_count: int
    accepted: bool


@dataclass(frozen=True)
class DeliveryCycleResult:
    scanned: int
    published: int
    blocked: int
    exhausted: int


def _utc(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise ValueError("delivery coordination time must include a timezone")
    return result.astimezone(UTC)


def _backoff_seconds(policy: DeliveryPolicy, retry_count: int) -> int:
    factor = 2 ** min(max(retry_count, 0), 20)
    return int(min(policy.backoff_max_seconds, policy.backoff_base_seconds * factor))


# 协调器按固定顺序锁定任务、尝试和投递记录，串行修改同一代投递状态。
def _locked_delivery_rows(
    session: Session, task_id: uuid.UUID, attempt_id: uuid.UUID
) -> tuple[Video, AnalysisTask, AnalysisAttempt, TaskDelivery] | None:
    candidate = session.get(AnalysisTask, task_id)
    if candidate is None:
        return None
    video = session.scalar(select(Video).where(Video.id == candidate.video_id).with_for_update())
    task = session.scalar(select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update())
    attempt = session.scalar(
        select(AnalysisAttempt)
        .where(AnalysisAttempt.id == attempt_id, AnalysisAttempt.task_id == task_id)
        .with_for_update()
    )
    delivery = session.scalar(
        select(TaskDelivery).where(TaskDelivery.attempt_id == attempt_id).with_for_update()
    )
    if video is None or task is None or attempt is None or delivery is None:
        return None
    return video, task, attempt, delivery


def _is_current_unclaimed(video: Video, task: AnalysisTask, attempt: AnalysisAttempt) -> bool:
    return bool(
        video.status == "ready"
        and video.media_status == "available"
        and video.current_analysis_task_id == task.id
        and task.video_id == video.id
        and task.status == "queued"
        and task.current_attempt_id == attempt.id
        and attempt.task_id == task.id
        and attempt.status == "queued"
        and attempt.claim_count == 0
        and attempt.execution_id is None
    )


def _active_execution_wait(session: Session) -> tuple[str, datetime] | None:
    blocking = find_unconfirmed_execution(session)
    if blocking is None:
        return None
    task = session.get(AnalysisTask, blocking.task_id)
    attempt = session.get(AnalysisAttempt, blocking.attempt_id)
    identity = identity_from_execution(blocking)
    if (
        task is not None
        and attempt is not None
        and task.status == "running"
        and attempt.status == "running"
        and blocking.supervisor_registered_at is not None
        and identity is not None
        and identity.process_scope == PROCESS_SCOPE
    ):
        return "execution_slot_occupied", blocking.started_at
    return "stop_confirmation_required", blocking.started_at


def current_delivery_wait_reason(
    session: Session,
    task: AnalysisTask,
    attempt: AnalysisAttempt,
    delivery: TaskDelivery,
    now: datetime,
) -> str | None:
    if task.status != "queued" or attempt.claim_count != 0:
        return None
    if (
        session.scalar(
            select(AnalysisExecution.id).where(AnalysisExecution.attempt_id == attempt.id).limit(1)
        )
        is not None
    ):
        return "stop_confirmation_required"
    active_execution = _active_execution_wait(session)
    if active_execution is not None:
        return active_execution[0]
    if task.delivery_failure_code is not None or task_delivery_exhausted(task, now):
        return "delivery_budget_exhausted"
    next_task_id = find_next_queued_task(session)
    if next_task_id is not None and next_task_id != task.id:
        return "queue_order_wait"
    if (
        task.delivery_wait_reason
        not in {
            "execution_slot_occupied",
            "stop_confirmation_required",
            "queue_order_wait",
        }
        and task.delivery_wait_reason is not None
    ):
        return task.delivery_wait_reason
    if delivery.operation_generation in {None, 0}:
        return "queued_for_delivery"
    if delivery.status == "pending":
        return "delivery_operation_pending"
    if delivery.status == "sent":
        return "delivery_claim_pending"
    return "delivery_retry_wait"


def _resume_budget_clock(task: AnalysisTask, now: datetime) -> None:
    if task.delivery_blocked_since is None:
        return
    if task.delivery_deadline_at is not None:
        paused_for = max(timedelta(0), now - task.delivery_blocked_since)
        task.delivery_deadline_at = task.delivery_deadline_at + paused_for
    task.delivery_blocked_since = None


def _pause_budget_clock(
    task: AnalysisTask,
    now: datetime,
    reason: str,
    policy: DeliveryPolicy,
    *,
    blocked_since: datetime | None = None,
) -> None:
    observed_start = blocked_since or now
    if task.delivery_blocked_since is None or observed_start < task.delivery_blocked_since:
        task.delivery_blocked_since = observed_start
    task.delivery_wait_reason = reason
    task.delivery_next_check_at = now + timedelta(seconds=policy.interval_seconds)


def _exhaust_delivery_budget(
    task: AnalysisTask,
    attempt: AnalysisAttempt,
    delivery: TaskDelivery,
    now: datetime,
) -> None:
    if task.delivery_retry_count is not None and task.delivery_retry_limit is not None:
        if task.delivery_retry_count >= task.delivery_retry_limit:
            code = "ANALYSIS_DELIVERY_RETRIES_EXHAUSTED"
        else:
            code = "ANALYSIS_DELIVERY_DEADLINE_EXPIRED"
    else:
        code = "ANALYSIS_DELIVERY_BUDGET_UNAVAILABLE"
    task.status = "failed"
    task.stage = "failed"
    task.failure_code = code
    task.delivery_failure_code = code
    task.delivery_wait_reason = "delivery_budget_exhausted"
    task.delivery_next_check_at = None
    task.delivery_blocked_since = None
    attempt.status = "failed"
    attempt.failure_code = code
    attempt.finished_at = attempt.finished_at or now
    delivery.status = "failed"
    delivery.error_code = code
    delivery.confirmed_at = None
    delivery.updated_at = now


# 预留时先在短事务中递增代次并记录期限，提交后才调用消息代理。
def _reserve_delivery(
    factory: sessionmaker[Session],
    task_id: uuid.UUID,
    attempt_id: uuid.UUID,
    *,
    now: datetime,
    policy: DeliveryPolicy,
) -> tuple[DeliveryReservation | None, str]:
    with db_transaction(factory) as session:
        rows = _locked_delivery_rows(session, task_id, attempt_id)
        if rows is None:
            return None, "not_found"
        video, task, attempt, delivery = rows
        if not _is_current_unclaimed(video, task, attempt):
            return None, "not_eligible"

        if not try_acquire_execution_gate(session):
            active_execution = _active_execution_wait(session)
            blocked_reason = (
                active_execution[0] if active_execution is not None else "execution_slot_occupied"
            )
            blocked_since = active_execution[1] if active_execution is not None else now
            _pause_budget_clock(task, now, blocked_reason, policy, blocked_since=blocked_since)
            return None, "blocked"

        # A started execution is never made eligible again by a delivery retry.
        execution = session.scalar(
            select(AnalysisExecution).where(AnalysisExecution.attempt_id == attempt.id).limit(1)
        )
        if execution is not None:
            _pause_budget_clock(
                task,
                now,
                "stop_confirmation_required",
                policy,
                blocked_since=execution.started_at,
            )
            return None, "blocked"

        active_execution = _active_execution_wait(session)
        if active_execution is not None:
            blocked_reason, blocked_since = active_execution
            _pause_budget_clock(task, now, blocked_reason, policy, blocked_since=blocked_since)
            return None, "blocked"

        next_task_id = find_next_queued_task(session)
        if next_task_id is not None and next_task_id != task.id:
            _pause_budget_clock(task, now, "queue_order_wait", policy)
            return None, "queue_order_wait"

        _resume_budget_clock(task, now)
        if (
            task.queue_entered_at is None
            or task.delivery_retry_count is None
            or task.delivery_retry_limit is None
            or task.delivery_operation_count is None
            or delivery.operation_generation is None
        ):
            return None, "not_coordinated"

        if task.delivery_next_check_at is not None and now < task.delivery_next_check_at:
            return None, "not_due"

        budget_expired = task.delivery_deadline_at is not None and now >= task.delivery_deadline_at
        retries_exhausted = task.delivery_retry_count >= task.delivery_retry_limit
        if budget_expired or retries_exhausted:
            _exhaust_delivery_budget(task, attempt, delivery, now)
            return None, "exhausted"

        is_retry = task.delivery_operation_count > 0
        if is_retry:
            task.delivery_retry_count += 1
        task.delivery_operation_count += 1
        task.delivery_deadline_at = task.delivery_deadline_at or timedelta_deadline(
            now, policy.deadline_seconds
        )
        generation = delivery.operation_generation + 1
        delivery.operation_generation = generation
        delivery.status = "pending"
        delivery.publish_started_at = now
        delivery.confirmed_at = None
        delivery.error_code = None
        delivery.updated_at = now
        task.delivery_next_check_at = now + timedelta(seconds=policy.operation_timeout_seconds)
        task.delivery_wait_reason = "delivery_operation_pending"
        record_analysis_event(
            session,
            task_id=task.id,
            event_key=f"delivery-reserved:{attempt.id}:{generation}",
            event_type="delivery_reserved",
            attempt_id=attempt.id,
            stage=task.stage,
        )
        return (
            DeliveryReservation(
                task_id=task.id,
                attempt_id=attempt.id,
                generation=generation,
                retry_count=task.delivery_retry_count,
                accepted=True,
            ),
            "reserved",
        )


def timedelta_deadline(now: datetime, deadline_seconds: int) -> datetime:
    return now + timedelta(seconds=deadline_seconds)


# 回写结果前核对预留代次，迟到的旧结果不能覆盖更新一轮投递状态。
def _record_delivery_outcome(
    factory: sessionmaker[Session],
    reservation: DeliveryReservation,
    *,
    outcome: DeliveryOutcome,
    error_code: str | None,
    now: datetime,
    policy: DeliveryPolicy,
) -> bool:
    with db_transaction(factory) as session:
        rows = _locked_delivery_rows(session, reservation.task_id, reservation.attempt_id)
        if rows is None:
            return False
        video, task, attempt, delivery = rows
        if (
            delivery.operation_generation != reservation.generation
            or delivery.status != "pending"
            or not _is_current_unclaimed(video, task, attempt)
        ):
            return False
        delivery.status = outcome
        delivery.error_code = error_code
        delivery.confirmed_at = now if outcome == "sent" else None
        delivery.updated_at = now
        task.delivery_wait_reason = "delivery_claim_pending" if outcome == "sent" else "delivery_retry_wait"
        task.delivery_next_check_at = now + timedelta(
            seconds=_backoff_seconds(policy, reservation.retry_count)
        )
        record_analysis_event(
            session,
            task_id=task.id,
            event_key=f"delivery-outcome:{attempt.id}:{reservation.generation}",
            event_type="delivery_sent" if outcome == "sent" else "delivery_retry_scheduled",
            attempt_id=attempt.id,
            stage=task.stage,
            error_code=error_code,
        )
        return True


# 先持久化发送意图再发布消息；结果不确定时留给后续协调扫描补投。
def coordinate_analysis_delivery(
    factory: sessionmaker[Session],
    task_id: uuid.UUID,
    attempt_id: uuid.UUID,
    publisher: Publisher,
    *,
    now: datetime | None = None,
    policy: DeliveryPolicy | None = None,
) -> str:
    """Reserve one durable send operation, publish outside the transaction, then CAS its result."""
    timestamp = _utc(now)
    delivery_policy = policy or DeliveryPolicy()
    reservation, state = _reserve_delivery(
        factory, task_id, attempt_id, now=timestamp, policy=delivery_policy
    )
    if reservation is None:
        return state
    code: str | None
    try:
        # Broker 确认可能丢失；generation 会记录不确定结果，后续补投扫描负责收敛。
        publisher(str(task_id), str(attempt_id))
    except Exception as exc:
        if isinstance(exc, EncodeError):
            outcome: DeliveryOutcome = "failed"
            code = "BROKER_PAYLOAD_REJECTED"
        else:
            outcome = "unknown"
            code = "BROKER_CONFIRMATION_UNKNOWN"
        logger.warning(
            "analysis_delivery_publish_unconfirmed task_id=%s generation=%s reason=%s",
            task_id.hex,
            reservation.generation,
            code,
        )
    else:
        outcome = "sent"
        code = None
    try:
        written = _record_delivery_outcome(
            factory,
            reservation,
            outcome=outcome,
            error_code=code,
            now=timestamp,
            policy=delivery_policy,
        )
    except SQLAlchemyError:
        logger.warning(
            "analysis_delivery_result_unrecorded task_id=%s generation=%s",
            task_id.hex,
            reservation.generation,
        )
        return "result_unrecorded"
    return "sent" if outcome == "sent" and written else outcome if written else "superseded"


# 按到期时间批量处理待投递任务，并限制每轮扫描数量。
def coordinate_due_analysis_deliveries(
    factory: sessionmaker[Session],
    publisher: Publisher,
    *,
    now: datetime | None = None,
    policy: DeliveryPolicy | None = None,
) -> DeliveryCycleResult:
    timestamp = _utc(now)
    delivery_policy = policy or DeliveryPolicy()
    with factory() as session:
        candidates = list(
            session.execute(
                select(AnalysisTask.id, AnalysisTask.current_attempt_id)
                .where(
                    AnalysisTask.status == "queued",
                    AnalysisTask.queue_entered_at.is_not(None),
                    AnalysisTask.delivery_retry_count.is_not(None),
                    AnalysisTask.delivery_operation_count.is_not(None),
                    or_(
                        AnalysisTask.delivery_next_check_at.is_(None),
                        AnalysisTask.delivery_next_check_at <= timestamp,
                    ),
                )
                .order_by(AnalysisTask.queue_entered_at, AnalysisTask.id)
                .limit(delivery_policy.scan_limit)
            ).all()
        )
    published = blocked = exhausted = 0
    for task_id, attempt_id in candidates:
        if attempt_id is None:
            continue
        outcome = coordinate_analysis_delivery(
            factory, task_id, attempt_id, publisher, now=timestamp, policy=delivery_policy
        )
        published += outcome in {"sent", "unknown", "failed", "result_unrecorded"}
        blocked += outcome == "blocked"
        exhausted += outcome == "exhausted"
    return DeliveryCycleResult(
        scanned=len(candidates),
        published=int(published),
        blocked=int(blocked),
        exhausted=int(exhausted),
    )


def task_delivery_exhausted(task: AnalysisTask, now: datetime) -> bool:
    return bool(
        task.delivery_failure_code is not None
        or (
            task.delivery_retry_count is not None
            and task.delivery_retry_limit is not None
            and task.delivery_retry_count >= task.delivery_retry_limit
        )
        or (
            task.delivery_deadline_at is not None
            and task.delivery_blocked_since is None
            and now >= task.delivery_deadline_at
        )
    )


def initialize_legacy_delivery_budget(task: AnalysisTask, now: datetime, policy: DeliveryPolicy) -> None:
    """Explicit user recovery may enroll a legacy task once; migration never enrolls history."""
    if task.queue_entered_at is not None:
        return
    task.queue_entered_at = now
    task.delivery_next_check_at = now
    task.delivery_retry_count = 0
    task.delivery_retry_limit = policy.retry_limit
    task.delivery_operation_count = 0
    task.delivery_deadline_at = None
    task.delivery_blocked_since = None
    task.delivery_wait_reason = "queued_for_delivery"
    task.delivery_failure_code = None


def _has_delivery_budget(task: AnalysisTask) -> bool:
    """Queue eligibility may be cleared while a durable wait keeps its budget."""
    return all(
        value is not None
        for value in (
            task.delivery_retry_count,
            task.delivery_retry_limit,
            task.delivery_operation_count,
        )
    )


def prepare_task_for_new_attempt(task: AnalysisTask, now: datetime, policy: DeliveryPolicy) -> str | None:
    """Re-enter queue order without clearing a Stage-4 budget or deadline."""
    if not _has_delivery_budget(task):
        initialize_legacy_delivery_budget(task, now, policy)
    else:
        _resume_budget_clock(task, now)
        if task_delivery_exhausted(task, now):
            return task.delivery_failure_code or (
                "ANALYSIS_DELIVERY_RETRIES_EXHAUSTED"
                if task.delivery_retry_count is not None
                and task.delivery_retry_limit is not None
                and task.delivery_retry_count >= task.delivery_retry_limit
                else "ANALYSIS_DELIVERY_DEADLINE_EXPIRED"
            )
        task.queue_entered_at = now
        task.delivery_next_check_at = now
        task.delivery_wait_reason = "queued_for_delivery"
    return None
