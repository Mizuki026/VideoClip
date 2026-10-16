from __future__ import annotations

# 本模块统一处理分析尝试的续跑与恢复，保证新尝试只从已核实的父尝试派生。
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from videoclip.analysis import (
    TASK_CONFIGURATION_VERSION,
    RecoverySubmission,
    _copy_complete_transcript,
    _transcript_rows,
    _validated_transcript,
    build_source_fingerprint,
    canonical_sha256,
)
from videoclip.analysis_delivery import DeliveryPolicy, prepare_task_for_new_attempt, task_delivery_exhausted
from videoclip.analysis_wait import (
    enter_model_wait,
    finish_model_wait,
    require_manual_wait,
    wait_budget_error,
)
from videoclip.events import record_analysis_event
from videoclip.execution_gate import find_unconfirmed_execution
from videoclip.model_cooling import (
    ModelIdentity,
    current_cooldown_until,
    model_identity_from_config,
    require_utc,
)
from videoclip.models import (
    AnalysisAttempt,
    AnalysisExecution,
    AnalysisSummary,
    AnalysisTask,
    SummaryRequest,
    TaskDelivery,
    Video,
)
from videoclip.recovery_rules import (
    AUTOMATIC_RECOVERY_FAILURES,
    automatic_recovery_budget_available,
    is_automatically_recoverable_failure,
    is_manual_recovery_blocked,
)
from videoclip.services import db_transaction
from videoclip.settings import Settings
from videoclip.stop_confirmation import PROCESS_SCOPE, identity_from_execution
from videoclip.summarization import MAX_SUMMARY_REQUESTS, SummaryFailure, resolve_summary_contract

FollowupMode = Literal["manual_recovery", "manual_summary", "automatic_recovery", "wait_resume"]


@dataclass(frozen=True)
class FollowupResult:
    submission: RecoverySubmission
    queued: bool
    state: str


def _blocked(code: str, *, summary: bool = False) -> HTTPException:
    detail = (
        {"code": code, "summary_available": False} if summary else {"code": code, "recovery_eligible": False}
    )
    return HTTPException(status_code=409, detail=detail)


def _attempt_identity(snapshot: object, *, stage: str, settings: Settings | None) -> ModelIdentity:
    if not isinstance(snapshot, dict):
        raise ValueError("invalid frozen attempt configuration")
    section_name = "asr" if stage == "transcribing" else "summary"
    section = snapshot.get(section_name)
    if not isinstance(section, dict):
        raise ValueError("missing frozen model configuration")
    return model_identity_from_config(
        section,
        default_account_reference=(
            settings.siliconflow_account_reference if settings is not None else "primary"
        ),
    )


def _source_is_current(video: Video, parent: AnalysisAttempt) -> bool:
    snapshot = parent.config_snapshot
    source = snapshot.get("source") if isinstance(snapshot, dict) else None
    try:
        compatible = bool(
            isinstance(snapshot, dict)
            and snapshot.get("version") == TASK_CONFIGURATION_VERSION
            and canonical_sha256(snapshot) == parent.config_sha256
            and isinstance(source, dict)
            and source.get("fingerprint") == parent.source_fingerprint
            and source.get("fingerprint")
            == build_source_fingerprint(video, int(source["audio_stream_index"]))
            and source.get("storage_name") == video.storage_name
            and source.get("byte_count") == video.byte_count
            and source.get("sha256") == video.content_sha256
            and source.get("duration_seconds") == video.duration_seconds
        )
        if not compatible or not isinstance(snapshot, dict):
            return False
        resolve_summary_contract(snapshot)
        return True
    except (KeyError, TypeError, ValueError, SummaryFailure):
        return False


# 只接受与当前尝试和执行身份完全匹配的监督器或容器退出证据。
def _verified_stop(
    session: Session,
    task: AnalysisTask,
    parent: AnalysisAttempt,
    settings: Settings | None,
) -> bool:
    if parent.claim_count != 1 or parent.execution_id is None or parent.stop_confirmed_at is None:
        return False
    execution = session.scalar(
        select(AnalysisExecution)
        .where(
            AnalysisExecution.id == parent.execution_id,
            AnalysisExecution.task_id == task.id,
            AnalysisExecution.attempt_id == parent.id,
        )
        .with_for_update()
    )
    identity = identity_from_execution(execution) if execution is not None else None
    if execution is None or execution.stop_confirmed_at is None:
        return False
    if execution.stop_confirmation_source == "test_inline_synthetic" and settings is not None:
        if settings.app_env != "test":
            return False
    elif (
        execution.stop_confirmation_source not in {"worker_supervisor", "operator_container_exit"}
        or not isinstance(execution.stop_evidence_sha256, str)
        or len(execution.stop_evidence_sha256) != 64
        or any(character not in "0123456789abcdef" for character in execution.stop_evidence_sha256)
        or identity is None
        or identity.process_scope != PROCESS_SCOPE
    ):
        return False
    return find_unconfirmed_execution(session) is None


# 只有用户明确发起的操作才登记为后续尝试，并保留原任务的配置快照。
def _enroll_explicit_task(task: AnalysisTask, now: datetime, settings: Settings | None) -> None:
    if task.stage5_enrolled_at is None:
        task.stage5_enrolled_at = now
        task.auto_recovery_count = 0
        task.auto_recovery_limit = 2
        task.model_wait_total_seconds = 0
        task.model_wait_total_limit_seconds = (
            settings.model_wait_total_limit_seconds if settings is not None else 259_200
        )


def _manual_permanent_error(parent: AnalysisAttempt, task: AnalysisTask) -> str | None:
    code = parent.failure_code or task.failure_code
    return code if is_manual_recovery_blocked(code) else None


# 自动恢复、手动恢复和摘要续跑统一在事务中核对旧尝试后创建新尝试。
def create_followup_attempt(
    factory: sessionmaker[Session],
    *,
    task_id: uuid.UUID,
    video_id: uuid.UUID,
    parent_attempt_id: uuid.UUID,
    mode: FollowupMode,
    owner_id: uuid.UUID | None = None,
    settings: Settings | None,
    now: datetime | None = None,
    delivery_policy: DeliveryPolicy | None = None,
) -> FollowupResult:
    """Serialize manual, automatic, and due-wait continuation through one transaction."""
    timestamp = require_utc(now or datetime.now(UTC))
    policy = delivery_policy or (
        DeliveryPolicy.from_settings(settings) if settings is not None else DeliveryPolicy()
    )
    with db_transaction(factory) as session:
        # 按视频、任务、父尝试逐级加锁，避免并发恢复重复创建子尝试或跨任务续跑。
        video_query = select(Video).where(Video.id == video_id)
        if owner_id is not None:
            video_query = video_query.where(Video.owner_id == owner_id)
        video = session.scalar(video_query.with_for_update())
        task = session.scalar(select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update())
        parent = session.scalar(
            select(AnalysisAttempt)
            .where(AnalysisAttempt.id == parent_attempt_id, AnalysisAttempt.task_id == task_id)
            .with_for_update()
        )
        if video is None or task is None or parent is None or task.video_id != video.id:
            if mode.startswith("manual"):
                raise HTTPException(status_code=404, detail="analysis task not found")
            return FollowupResult(RecoverySubmission(task_id, parent_attempt_id, False), False, "not_found")

        if (
            video.status != "ready"
            or video.media_status != "available"
            or video.current_analysis_task_id != task.id
        ):
            if mode.startswith("manual"):
                code = (
                    "VIDEO_MEDIA_UNAVAILABLE"
                    if video.media_status != "available"
                    else "VIDEO_DELETION_IN_PROGRESS"
                )
                raise _blocked(code, summary=mode == "manual_summary")
            return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "video_unavailable")

        existing_child = session.scalar(
            select(AnalysisAttempt).where(AnalysisAttempt.parent_attempt_id == parent.id)
        )
        if existing_child is not None:
            delivery = session.get(TaskDelivery, existing_child.id)
            if delivery is None:
                if mode.startswith("manual"):
                    raise HTTPException(status_code=503, detail="attempt delivery state needs maintenance")
                return FollowupResult(
                    RecoverySubmission(task.id, existing_child.id, False), False, "delivery_missing"
                )
            is_queued = bool(
                task.current_attempt_id == existing_child.id
                and task.status == "queued"
                and existing_child.status == "queued"
                and existing_child.claim_count == 0
            )
            return FollowupResult(
                RecoverySubmission(task.id, existing_child.id, False),
                is_queued,
                "existing_child",
            )

        if task.current_attempt_id != parent.id:
            if mode.startswith("manual"):
                raise _blocked("ATTEMPT_NOT_CURRENT", summary=mode == "manual_summary")
            return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "not_current")

        if mode == "wait_resume":
            if (
                task.status != "waiting_model"
                or parent.status != "waiting_model"
                or task.delivery_next_check_at is None
                or require_utc(task.delivery_next_check_at) > timestamp
            ):
                return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "not_due")
            origin = task.model_wait_origin
            if origin == "automatic_recovery":
                mode_for_eligibility = "automatic_recovery"
            elif origin in {"manual_recovery", "continuation"}:
                mode_for_eligibility = "wait_resume"
            else:
                require_manual_wait(task, parent, "MODEL_WAIT_STATE_INVALID")
                return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "manual_required")
        else:
            mode_for_eligibility = mode

        if mode == "manual_summary":
            if task.status != "awaiting_summary" or parent.status != "awaiting_summary":
                raise _blocked("SUMMARY_CONTINUATION_NOT_ELIGIBLE", summary=True)
            if task.summary_status != "waiting" or session.get(AnalysisSummary, task.id) is not None:
                raise _blocked("SUMMARY_CONTINUATION_NOT_ELIGIBLE", summary=True)
        elif mode == "manual_recovery":
            if task.status == "running" or parent.status == "running":
                if not _verified_stop(session, task, parent, settings):
                    raise _blocked("STOP_CONFIRMATION_REQUIRED")
            if task.status != "failed" or parent.status != "failed":
                raise _blocked("ATTEMPT_NOT_RECOVERABLE")
            permanent = _manual_permanent_error(parent, task)
            if permanent:
                raise _blocked(permanent)
        elif mode_for_eligibility == "automatic_recovery":
            if (
                task.status not in {"failed", "waiting_model"}
                or parent.status not in {"failed", "waiting_model"}
                or task.stage5_enrolled_at is None
                or not is_automatically_recoverable_failure(parent.failure_code or task.failure_code)
            ):
                return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "not_recoverable")
            if not automatic_recovery_budget_available(task.auto_recovery_count, task.auto_recovery_limit):
                task.delivery_wait_reason = "automatic_recovery_exhausted"
                task.delivery_next_check_at = None
                return FollowupResult(
                    RecoverySubmission(task.id, parent.id, False), False, "recovery_exhausted"
                )
        elif mode_for_eligibility == "wait_resume":
            if not _verified_stop(session, task, parent, settings):
                task.delivery_wait_reason = "stop_confirmation_required"
                task.delivery_next_check_at = timestamp + timedelta(seconds=policy.interval_seconds)
                return FollowupResult(
                    RecoverySubmission(task.id, parent.id, False), False, "stop_confirmation_required"
                )
        # 创建后续 attempt 前必须先把当前执行的可信停止证据写入数据库。
        if mode_for_eligibility in {"automatic_recovery", "manual_recovery", "manual_summary", "wait_resume"}:
            if not _verified_stop(session, task, parent, settings):
                if mode.startswith("manual"):
                    raise _blocked("STOP_CONFIRMATION_REQUIRED", summary=mode == "manual_summary")
                task.delivery_wait_reason = "stop_confirmation_required"
                task.delivery_next_check_at = timestamp + timedelta(seconds=policy.interval_seconds)
                return FollowupResult(
                    RecoverySubmission(task.id, parent.id, False), False, "stop_confirmation_required"
                )

        delivery = session.get(TaskDelivery, parent.id)
        if delivery is None:
            if mode.startswith("manual"):
                raise HTTPException(status_code=503, detail="attempt delivery state needs maintenance")
            return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "delivery_missing")
        if task_delivery_exhausted(task, timestamp):
            code = task.delivery_failure_code or "ANALYSIS_DELIVERY_BUDGET_EXHAUSTED"
            if mode.startswith("manual"):
                raise _blocked(code, summary=mode == "manual_summary")
            task.delivery_wait_reason = "delivery_budget_exhausted"
            task.delivery_next_check_at = None
            return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "delivery_exhausted")
        if not _source_is_current(video, parent):
            if mode.startswith("manual"):
                raise _blocked("SOURCE_CONFIGURATION_INCOMPATIBLE", summary=mode == "manual_summary")
            task.delivery_wait_reason = "source_configuration_incompatible"
            task.delivery_next_check_at = None
            task.failure_code = task.failure_code or "SOURCE_CONFIGURATION_INCOMPATIBLE"
            return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "source_mismatch")

        source_rows = _transcript_rows(session, parent.id)
        summary_only = bool(
            parent.manifest_fingerprint
            and source_rows
            and all(item["status"] == "succeeded" and item["text"] for item in source_rows)
        )
        if summary_only:
            try:
                _validated_transcript(session, video, parent)
            except HTTPException:
                if mode.startswith("manual"):
                    raise
                task.delivery_wait_reason = "transcript_checkpoint_invalid"
                task.delivery_next_check_at = None
                task.failure_code = task.failure_code or "TRANSCRIPT_CHECKPOINT_INVALID"
                return FollowupResult(
                    RecoverySubmission(task.id, parent.id, False), False, "checkpoint_invalid"
                )
        if mode == "manual_summary" and not summary_only:
            raise _blocked("TRANSCRIPT_INCOMPLETE", summary=True)

        summary_count = int(
            session.scalar(
                select(func.count()).select_from(SummaryRequest).where(SummaryRequest.task_id == task.id)
            )
            or 0
        )
        if summary_count >= MAX_SUMMARY_REQUESTS:
            if mode.startswith("manual"):
                raise _blocked("SUMMARY_REQUEST_BUDGET_EXHAUSTED", summary=mode == "manual_summary")
            task.delivery_wait_reason = "summary_request_budget_exhausted"
            task.delivery_next_check_at = None
            task.failure_code = task.failure_code or "SUMMARY_REQUEST_BUDGET_EXHAUSTED"
            return FollowupResult(
                RecoverySubmission(task.id, parent.id, False), False, "summary_budget_exhausted"
            )

        if mode in {"manual_recovery", "manual_summary"}:
            _enroll_explicit_task(task, timestamp, settings)

        stage = "summarizing" if summary_only else "transcribing"
        try:
            identity = _attempt_identity(parent.config_snapshot, stage=stage, settings=settings)
        except ValueError:
            if mode.startswith("manual"):
                raise _blocked("MODEL_CONFIGURATION_UNAVAILABLE", summary=mode == "manual_summary") from None
            task.delivery_wait_reason = "model_configuration_unavailable"
            task.delivery_next_check_at = None
            return FollowupResult(
                RecoverySubmission(task.id, parent.id, False), False, "model_configuration_invalid"
            )
        cooldown_until = current_cooldown_until(session, identity)
        if cooldown_until is not None and cooldown_until > timestamp:
            wait_error = wait_budget_error(
                task=task,
                now=timestamp,
                cooldown_until=cooldown_until,
                single_limit_seconds=(
                    settings.model_wait_single_limit_seconds if settings is not None else 86_400
                ),
                total_limit_seconds=(
                    task.model_wait_total_limit_seconds
                    or (settings.model_wait_total_limit_seconds if settings is not None else 259_200)
                ),
            )
            if wait_error is not None:
                require_manual_wait(task, parent, wait_error)
                return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "manual_required")
            enter_model_wait(
                session,
                task,
                parent,
                identity=identity,
                stage=stage,
                origin=(
                    "automatic_recovery"
                    if mode_for_eligibility == "automatic_recovery"
                    else task.model_wait_origin
                    or ("manual_recovery" if mode == "manual_recovery" else "continuation")
                ),
                cooldown_until=cooldown_until,
                now=timestamp,
                single_limit_seconds=(
                    settings.model_wait_single_limit_seconds if settings is not None else 86_400
                ),
                total_limit_seconds=(
                    task.model_wait_total_limit_seconds
                    or (settings.model_wait_total_limit_seconds if settings is not None else 259_200)
                ),
            )
            return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "cooldown_active")

        if mode == "wait_resume":
            finish_model_wait(task, now=timestamp)
        budget_error = prepare_task_for_new_attempt(task, timestamp, policy)
        if budget_error is not None:
            if mode.startswith("manual"):
                raise _blocked(budget_error, summary=mode == "manual_summary")
            task.delivery_wait_reason = "delivery_budget_exhausted"
            task.delivery_next_check_at = None
            return FollowupResult(RecoverySubmission(task.id, parent.id, False), False, "delivery_exhausted")

        if mode == "manual_summary" and summary_only:
            # The parent remains the durable source of its completed transcript.
            parent.status = "awaiting_summary"
        elif parent.claim_count == 0:
            parent.failure_code = "ATTEMPT_SUPERSEDED"
            parent.status = "failed"
        else:
            parent.failure_code = parent.failure_code or "ATTEMPT_RECOVERED"
            parent.status = "failed"
        parent.finished_at = parent.finished_at or timestamp
        next_number = (
            int(
                session.scalar(
                    select(func.coalesce(func.max(AnalysisAttempt.attempt_number), 0)).where(
                        AnalysisAttempt.task_id == task.id
                    )
                )
                or 0
            )
            + 1
        )
        child = AnalysisAttempt(
            task_id=task.id,
            parent_attempt_id=parent.id,
            attempt_number=next_number,
            status="queued",
            execution_mode="summary_only" if summary_only else "full",
            config_snapshot=parent.config_snapshot,
            config_sha256=parent.config_sha256,
            source_fingerprint=parent.source_fingerprint,
            work_directory_name=uuid.uuid4().hex,
            claim_count=0,
            cleanup_state="pending",
        )
        session.add(child)
        session.flush()
        if summary_only:
            _copy_complete_transcript(session, parent, child)
        task.current_attempt_id = child.id
        task.status = "queued"
        task.stage = "queued_summary" if summary_only else "queued"
        task.summary_status = "waiting" if summary_only else "not_started"
        task.failure_code = None
        if mode_for_eligibility == "automatic_recovery":
            task.auto_recovery_count = (task.auto_recovery_count or 0) + 1
        session.add(TaskDelivery(attempt_id=child.id, status="pending", operation_generation=0))
        session.flush()
        if mode_for_eligibility == "automatic_recovery":
            event_type = "automatic_recovery_created"
            actor = "system"
        elif mode == "manual_recovery":
            event_type = "manual_recovery_created"
            actor = "user"
        elif mode == "manual_summary":
            event_type = "manual_continuation_created"
            actor = "user"
        else:
            event_type = "model_wait_resumed"
            actor = "system"
        record_analysis_event(
            session,
            task_id=task.id,
            event_key=f"followup-created:{child.id}",
            event_type=event_type,
            attempt_id=child.id,
            stage=task.stage,
            actor=actor,
        )
        return FollowupResult(RecoverySubmission(task.id, child.id, True), True, "created")


@dataclass(frozen=True)
class RecoveryCycleResult:
    scanned: int
    resumed: int
    waiting: int
    blocked: int
    exhausted: int


# 扫描到期恢复候选；每个候选仍须重新通过状态、预算和停止证据核验。
def coordinate_due_analysis_recoveries(
    factory: sessionmaker[Session],
    *,
    settings: Settings,
    now: datetime | None = None,
    delivery_policy: DeliveryPolicy | None = None,
    limit: int = 4,
) -> RecoveryCycleResult:
    """Boundedly resume due model waits and confirmed recoverable failures."""
    timestamp = require_utc(now or datetime.now(UTC))
    policy = delivery_policy or DeliveryPolicy.from_settings(settings)
    if not 1 <= limit <= 8:
        raise ValueError("recovery scan limit must be between 1 and 8")
    eligible_errors = sorted(AUTOMATIC_RECOVERY_FAILURES)
    with factory() as session:
        candidates = list(
            session.execute(
                select(AnalysisTask.id, AnalysisTask.video_id, AnalysisTask.current_attempt_id)
                .where(
                    AnalysisTask.current_attempt_id.is_not(None),
                    AnalysisTask.delivery_next_check_at.is_not(None),
                    AnalysisTask.delivery_next_check_at <= timestamp,
                    AnalysisTask.stage5_enrolled_at.is_not(None),
                    (
                        (AnalysisTask.status == "waiting_model")
                        | (
                            (AnalysisTask.status == "failed")
                            & AnalysisTask.failure_code.in_(eligible_errors)
                            & AnalysisTask.auto_recovery_count.is_not(None)
                            & AnalysisTask.auto_recovery_limit.is_not(None)
                        )
                    ),
                )
                .order_by(AnalysisTask.delivery_next_check_at, AnalysisTask.queue_entered_at, AnalysisTask.id)
                .limit(limit)
            )
        )
        session.commit()

    resumed = waiting = blocked = exhausted = 0
    for task_id, video_id, attempt_id in candidates:
        if attempt_id is None:
            continue
        with factory() as session:
            status_value = session.scalar(select(AnalysisTask.status).where(AnalysisTask.id == task_id))
            session.commit()
        mode: FollowupMode = "wait_resume" if status_value == "waiting_model" else "automatic_recovery"
        result = create_followup_attempt(
            factory,
            task_id=task_id,
            video_id=video_id,
            parent_attempt_id=attempt_id,
            mode=mode,
            settings=settings,
            now=timestamp,
            delivery_policy=policy,
        )
        if result.queued and result.submission.created:
            resumed += 1
        elif result.state in {"cooldown_active", "not_due"}:
            waiting += 1
        elif result.state in {"recovery_exhausted", "summary_budget_exhausted", "delivery_exhausted"}:
            exhausted += 1
        else:
            blocked += 1
    return RecoveryCycleResult(len(candidates), resumed, waiting, blocked, exhausted)
