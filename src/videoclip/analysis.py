from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from videoclip.analysis_delivery import (
    DeliveryPolicy,
    current_delivery_wait_reason,
)
from videoclip.models import (
    AnalysisAttempt,
    AnalysisExecution,
    AnalysisSummary,
    AnalysisTask,
    SummaryRequest,
    TaskDelivery,
    TranscriptSegment,
    Video,
)
from videoclip.recovery_rules import evaluate_recovery, is_manual_recovery_blocked
from videoclip.schemas import (
    AnalysisAttemptResponse,
    AnalysisSummaryResponse,
    AnalysisTaskResponse,
    TranscriptResponse,
    TranscriptSegmentResponse,
)
from videoclip.services import db_transaction
from videoclip.settings import Settings
from videoclip.summarization import (
    CHAT_MESSAGE_OVERHEAD_TOKENS,
    MAX_SUMMARY_REQUESTS,
    QUOTE_NORMALIZATION_VERSION,
    SUMMARY_REQUEST_CONTRACT_VERSION,
    SummaryFailure,
    resolve_summary_contract,
    response_schema_sha256,
    template_sha256,
    validate_transcript_manifest,
)

TASK_CONFIGURATION_VERSION = "analysis-config-v1"
SOURCE_FINGERPRINT_VERSION = "source-v1"
ASR_REQUEST_FINGERPRINT_VERSION = "asr-request-v1"


@dataclass(frozen=True)
class SubmittedTask:
    task_id: uuid.UUID
    attempt_id: uuid.UUID
    created: bool


@dataclass(frozen=True)
class RecoverySubmission:
    task_id: uuid.UUID
    attempt_id: uuid.UUID
    created: bool


def _transcript_rows(session: Session, attempt_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.attempt_id == attempt_id)
            .order_by(TranscriptSegment.segment_number)
        )
    )
    return [
        {
            "segment_number": row.segment_number,
            "audio_start_seconds": row.audio_start_seconds,
            "audio_end_seconds": row.audio_end_seconds,
            "actual_audio_seconds": row.actual_audio_seconds,
            "video_start_seconds": row.video_start_seconds,
            "video_end_seconds": row.video_end_seconds,
            "audio_sha256": row.audio_sha256,
            "request_fingerprint": row.request_fingerprint,
            "status": row.status,
            "text": row.text,
        }
        for row in rows
    ]


def _validated_transcript(
    session: Session, video: Video, attempt: AnalysisAttempt
) -> tuple[list[dict[str, Any]], str]:
    snapshot = attempt.config_snapshot
    source = snapshot.get("source") if isinstance(snapshot, dict) else None
    try:
        compatible = (
            snapshot.get("version") == TASK_CONFIGURATION_VERSION
            and canonical_sha256(snapshot) == attempt.config_sha256
            and isinstance(source, dict)
            and source.get("fingerprint") == attempt.source_fingerprint
            and source.get("fingerprint")
            == build_source_fingerprint(video, int(source["audio_stream_index"]))
            and source.get("storage_name") == video.storage_name
            and source.get("byte_count") == video.byte_count
            and source.get("sha256") == video.content_sha256
            and source.get("duration_seconds") == video.duration_seconds
        )
        resolve_summary_contract(snapshot)
    except (KeyError, TypeError, ValueError, SummaryFailure) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "SUMMARY_CONFIGURATION_INCOMPATIBLE"},
        ) from exc
    if not compatible or attempt.manifest_fingerprint is None:
        raise HTTPException(status_code=409, detail={"code": "TRANSCRIPT_SOURCE_MISMATCH"})
    rows = _transcript_rows(session, attempt.id)
    try:
        validated, transcript_fingerprint = validate_transcript_manifest(
            rows,
            snapshot=snapshot,
            video_duration_seconds=video.duration_seconds,
            expected_manifest_fingerprint=attempt.manifest_fingerprint,
        )
    except SummaryFailure as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    return validated, transcript_fingerprint


def _copy_complete_transcript(
    session: Session, source_attempt: AnalysisAttempt, target_attempt: AnalysisAttempt
) -> None:
    source_rows = list(
        session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.attempt_id == source_attempt.id)
            .order_by(TranscriptSegment.segment_number)
        )
    )
    for source in source_rows:
        session.add(
            TranscriptSegment(
                attempt_id=target_attempt.id,
                source_attempt_id=source_attempt.id,
                segment_number=source.segment_number,
                audio_start_seconds=source.audio_start_seconds,
                audio_end_seconds=source.audio_end_seconds,
                actual_audio_seconds=source.actual_audio_seconds,
                video_start_seconds=source.video_start_seconds,
                video_end_seconds=source.video_end_seconds,
                audio_sha256=source.audio_sha256,
                request_fingerprint=source.request_fingerprint,
                status="succeeded",
                text=source.text,
                request_count=0,
            )
        )
    target_attempt.manifest_fingerprint = source_attempt.manifest_fingerprint


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_source_fingerprint(video: Video, audio_stream_index: int) -> str:
    return canonical_sha256(
        {
            "version": SOURCE_FINGERPRINT_VERSION,
            "video_id": str(video.id),
            "storage_name": video.storage_name,
            "byte_count": video.byte_count,
            "sha256": video.content_sha256,
            "duration_seconds": video.duration_seconds,
            "audio_stream_index": audio_stream_index,
            "media_info": video.media_info,
        }
    )


def build_configuration_snapshot(video: Video, settings: Settings) -> dict[str, Any]:
    streams = video.media_info.get("streams", [])
    audio_stream = next(
        (item for item in streams if isinstance(item, dict) and item.get("type") == "audio"), None
    )
    if audio_stream is None or not isinstance(audio_stream.get("index"), int):
        raise HTTPException(status_code=409, detail="video has no verified audio stream")
    audio_stream_index = int(audio_stream["index"])
    snapshot = {
        "version": TASK_CONFIGURATION_VERSION,
        "source": {
            "storage_name": video.storage_name,
            "byte_count": video.byte_count,
            "sha256": video.content_sha256,
            "duration_seconds": video.duration_seconds,
            "audio_stream_index": audio_stream_index,
            "fingerprint": build_source_fingerprint(video, audio_stream_index),
        },
        "audio": {
            "sample_rate": settings.audio_sample_rate,
            "channels": settings.audio_channels,
            "sample_format": settings.audio_sample_format,
        },
        "slicing": {
            "manifest_version": settings.slice_manifest_version,
            "timeline_mapping_version": settings.timeline_mapping_version,
            "segment_seconds": settings.segment_seconds,
            "video_start_offset_seconds": 0.0,
        },
        "asr": {
            "provider": "siliconflow",
            "account_reference": settings.siliconflow_account_reference,
            "base_url": settings.asr_base_url,
            "model": settings.asr_model,
            "parameters": dict(settings.asr_request_parameters),
            "secret_reference": settings.asr_secret_reference,
        },
        "summary": {
            "provider": "siliconflow",
            "account_reference": settings.siliconflow_account_reference,
            "model": settings.llm_model,
            "prompt_version": settings.summary_prompt_version,
            "schema_version": settings.summary_schema_version,
            "context_tokens": settings.llm_context_tokens,
            "max_output_tokens": settings.llm_max_output_tokens,
            "request": {
                "contract_version": SUMMARY_REQUEST_CONTRACT_VERSION,
                "base_url": settings.asr_base_url,
                "temperature": settings.llm_temperature,
                "enable_thinking": settings.llm_enable_thinking,
                "timeouts": {
                    "total": settings.llm_http_total_timeout_seconds,
                    "connect": settings.llm_http_connect_timeout_seconds,
                    "read": settings.llm_http_read_timeout_seconds,
                    "write": settings.llm_http_write_timeout_seconds,
                    "pool": settings.llm_http_pool_timeout_seconds,
                },
                "max_response_bytes": settings.max_llm_response_bytes,
                "max_diagnostic_bytes": settings.max_llm_diagnostic_bytes,
                "max_requests": MAX_SUMMARY_REQUESTS,
                "context_overhead_tokens": CHAT_MESSAGE_OVERHEAD_TOKENS,
                "template_sha256": template_sha256(
                    settings.summary_prompt_version, settings.summary_schema_version
                ),
                "response_schema_sha256": response_schema_sha256(),
                "quote_normalization_version": QUOTE_NORMALIZATION_VERSION,
            },
        },
        "limits": {
            "attempt_total_seconds": settings.attempt_total_timeout_seconds,
            "media_total_seconds": settings.media_total_timeout_seconds,
            "process_stop_grace_seconds": settings.process_stop_grace_seconds,
            "http_total_seconds": settings.asr_http_total_timeout_seconds,
            "http_connect_seconds": settings.asr_http_connect_timeout_seconds,
            "http_read_seconds": settings.asr_http_read_timeout_seconds,
            "response_max_bytes": settings.max_asr_response_bytes,
            "diagnostic_max_bytes": settings.max_media_diagnostic_bytes,
        },
    }
    try:
        resolve_summary_contract(snapshot)
    except SummaryFailure as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "SUMMARY_CONFIGURATION_UNSUPPORTED"},
        ) from exc
    return snapshot


def submit_analysis_task(
    factory: sessionmaker[Session], owner_id: uuid.UUID, video_id: uuid.UUID, settings: Settings
) -> SubmittedTask:
    now = datetime.now(UTC)
    delivery_policy = DeliveryPolicy.from_settings(settings)
    with db_transaction(factory) as session:
        video = session.scalar(
            select(Video).where(Video.id == video_id, Video.owner_id == owner_id).with_for_update()
        )
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        if video.status != "ready":
            raise HTTPException(status_code=409, detail="video is not available for analysis")
        if video.media_status != "available":
            raise HTTPException(
                status_code=409,
                detail={"code": "VIDEO_MEDIA_UNAVAILABLE", "recovery_eligible": False},
            )

        if video.current_analysis_task_id is not None:
            task = session.get(AnalysisTask, video.current_analysis_task_id)
            if task is None:
                raise HTTPException(status_code=503, detail="analysis task association needs maintenance")
            attempt = session.get(AnalysisAttempt, task.current_attempt_id)
            if attempt is None:
                raise HTTPException(status_code=503, detail="analysis attempt needs maintenance")
            return SubmittedTask(task_id=task.id, attempt_id=attempt.id, created=False)

        snapshot = build_configuration_snapshot(video, settings)
        config_hash = canonical_sha256(snapshot)
        source_hash = snapshot["source"]["fingerprint"]
        task = AnalysisTask(
            video_id=video.id,
            owner_id=owner_id,
            status="queued",
            stage="queued",
            queue_entered_at=now,
            delivery_next_check_at=now,
            delivery_retry_count=0,
            delivery_retry_limit=delivery_policy.retry_limit,
            delivery_operation_count=0,
            delivery_wait_reason="queued_for_delivery",
            auto_recovery_count=0,
            auto_recovery_limit=2,
            stage5_enrolled_at=now,
            model_wait_total_seconds=0,
            model_wait_total_limit_seconds=settings.model_wait_total_limit_seconds,
        )
        session.add(task)
        session.flush()
        attempt = AnalysisAttempt(
            task_id=task.id,
            attempt_number=1,
            status="queued",
            config_snapshot=snapshot,
            config_sha256=config_hash,
            source_fingerprint=source_hash,
            work_directory_name=uuid.uuid4().hex,
            claim_count=0,
            cleanup_state="pending",
        )
        session.add(attempt)
        session.flush()
        task.current_attempt_id = attempt.id
        video.current_analysis_task_id = task.id
        video.analysis_task_created_at = now
        session.add(TaskDelivery(attempt_id=attempt.id, status="pending", operation_generation=0))
        session.flush()
        return SubmittedTask(task_id=task.id, attempt_id=attempt.id, created=True)


def recover_analysis_attempt(
    factory: sessionmaker[Session],
    owner_id: uuid.UUID,
    video_id: uuid.UUID,
    task_id: uuid.UUID,
    parent_attempt_id: uuid.UUID,
    policy: DeliveryPolicy | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> RecoverySubmission:
    from videoclip.analysis_lifecycle import create_followup_attempt

    with factory() as session:
        task_owner = session.scalar(
            select(AnalysisTask.owner_id).where(
                AnalysisTask.id == task_id,
                AnalysisTask.video_id == video_id,
            )
        )
        session.commit()
    if task_owner != owner_id:
        raise HTTPException(status_code=404, detail="analysis task not found")
    return create_followup_attempt(
        factory,
        task_id=task_id,
        video_id=video_id,
        parent_attempt_id=parent_attempt_id,
        mode="manual_recovery",
        owner_id=owner_id,
        settings=settings,
        delivery_policy=policy,
        now=now,
    ).submission


def continue_summary_attempt(
    factory: sessionmaker[Session],
    owner_id: uuid.UUID,
    video_id: uuid.UUID,
    task_id: uuid.UUID,
    parent_attempt_id: uuid.UUID,
    policy: DeliveryPolicy | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> RecoverySubmission:
    from videoclip.analysis_lifecycle import create_followup_attempt

    with factory() as session:
        task_owner = session.scalar(
            select(AnalysisTask.owner_id).where(
                AnalysisTask.id == task_id,
                AnalysisTask.video_id == video_id,
            )
        )
        session.commit()
    if task_owner != owner_id:
        raise HTTPException(status_code=404, detail="analysis task not found")
    return create_followup_attempt(
        factory,
        task_id=task_id,
        video_id=video_id,
        parent_attempt_id=parent_attempt_id,
        mode="manual_summary",
        owner_id=owner_id,
        settings=settings,
        delivery_policy=policy,
        now=now,
    ).submission


def task_response(
    factory: sessionmaker[Session],
    owner_id: uuid.UUID,
    video_id: uuid.UUID,
    task_id: uuid.UUID,
    attempt_id: uuid.UUID | None = None,
) -> AnalysisTaskResponse:
    with factory() as session:
        task = session.scalar(
            select(AnalysisTask).where(
                AnalysisTask.id == task_id,
                AnalysisTask.video_id == video_id,
                AnalysisTask.owner_id == owner_id,
            )
        )
        if task is None:
            raise HTTPException(status_code=404, detail="analysis task not found")
        video = session.get(Video, video_id)
        if video is None or video.status != "ready":
            raise HTTPException(
                status_code=409,
                detail={"code": "VIDEO_DELETION_IN_PROGRESS", "recovery_eligible": False},
            )
        current_attempt_id = task.current_attempt_id
        if current_attempt_id is None:
            raise HTTPException(status_code=503, detail="analysis task state needs maintenance")
        selected_attempt_id = attempt_id or current_attempt_id
        attempt = session.get(AnalysisAttempt, selected_attempt_id)
        if attempt is not None and attempt.task_id != task.id:
            attempt = None
        delivery = session.get(TaskDelivery, selected_attempt_id)
        if attempt is None or delivery is None:
            raise HTTPException(status_code=503, detail="analysis task state needs maintenance")
        expected, completed, request_count, elapsed_seconds, reused_count = session.execute(
            select(
                func.count(TranscriptSegment.id),
                func.count(TranscriptSegment.id).filter(TranscriptSegment.status == "succeeded"),
                func.coalesce(func.sum(TranscriptSegment.request_count), 0),
                func.coalesce(
                    func.sum(TranscriptSegment.request_metadata["elapsed_seconds"].as_float()), 0.0
                ),
                func.count(TranscriptSegment.id).filter(TranscriptSegment.source_attempt_id.is_not(None)),
            ).where(TranscriptSegment.attempt_id == attempt.id)
        ).one()
        call_rows = list(
            session.scalars(
                select(TranscriptSegment)
                .where(
                    TranscriptSegment.attempt_id == attempt.id,
                    TranscriptSegment.request_metadata.is_not(None),
                    TranscriptSegment.request_count > 0,
                )
                .order_by(TranscriptSegment.segment_number)
            )
        )
        execution = session.scalar(
            select(AnalysisExecution)
            .where(
                AnalysisExecution.attempt_id == attempt.id,
            )
            .limit(1)
        )
        summary_requests_used = int(
            session.scalar(
                select(func.count()).select_from(SummaryRequest).where(SummaryRequest.task_id == task.id)
            )
            or 0
        )
        is_current = task.current_attempt_id == attempt.id
        now = datetime.now(UTC)
        queue_wait_reason = current_delivery_wait_reason(session, task, attempt, delivery, now)
        delivery_budget_exhausted = (
            task.delivery_failure_code is not None or queue_wait_reason == "delivery_budget_exhausted"
        )
        delivery_failure_code = task.delivery_failure_code
        if delivery_budget_exhausted and delivery_failure_code is None:
            if (
                task.delivery_retry_count is not None
                and task.delivery_retry_limit is not None
                and task.delivery_retry_count >= task.delivery_retry_limit
            ):
                delivery_failure_code = "ANALYSIS_DELIVERY_RETRIES_EXHAUSTED"
            else:
                delivery_failure_code = "ANALYSIS_DELIVERY_DEADLINE_EXPIRED"
        eligibility = evaluate_recovery(
            video_status=video.status,
            task_status=task.status,
            attempt_status=attempt.status,
            delivery_status=delivery.status,
            claim_count=attempt.claim_count,
            delivery_updated_at=delivery.updated_at,
            attempt_stop_confirmed=attempt.stop_confirmed_at is not None,
            execution_stop_confirmed=(execution is not None and execution.stop_confirmed_at is not None),
            is_current_attempt=is_current,
            now=now,
            delivery_budget_exhausted=delivery_budget_exhausted,
        )
        stop_evidence_valid = bool(
            execution is not None
            and execution.stop_confirmed_at is not None
            and execution.stop_confirmation_source in {"worker_supervisor", "operator_container_exit"}
            and execution.stop_evidence_sha256 is not None
            and execution.process_scope == "linux-session-v1"
            and execution.worker_instance_id is not None
            and execution.worker_container_id is not None
            and execution.boundary_pid is not None
            and execution.boundary_start_ticks is not None
            and execution.boundary_session_id is not None
            and execution.boundary_boot_id is not None
        )
        recoverable_by_operator = not is_manual_recovery_blocked(attempt.failure_code or task.failure_code)
        recovery_allowed = (
            eligibility.eligible
            and video.media_status == "available"
            and stop_evidence_valid
            and recoverable_by_operator
            and summary_requests_used < MAX_SUMMARY_REQUESTS
        )
        wait_seconds_used = task.model_wait_total_seconds
        if wait_seconds_used is not None and task.model_wait_started_at is not None:
            wait_seconds_used += max(0, math.ceil((now - task.model_wait_started_at).total_seconds()))
        return AnalysisTaskResponse(
            id=task.id,
            video_id=task.video_id,
            current_attempt_id=current_attempt_id,
            notes_attempt_id=task.notes_attempt_id,
            status=task.status,
            stage=task.stage,
            delivery_status=delivery.status,
            delivery_updated_at=delivery.updated_at,
            delivery_error_code=delivery.error_code,
            delivery_operation_generation=delivery.operation_generation,
            queue_entered_at=task.queue_entered_at,
            delivery_next_check_at=task.delivery_next_check_at,
            delivery_retry_count=task.delivery_retry_count,
            delivery_retry_limit=task.delivery_retry_limit,
            delivery_deadline_at=task.delivery_deadline_at,
            delivery_wait_reason=queue_wait_reason,
            delivery_failure_code=delivery_failure_code,
            model_wait_reason=task.model_wait_reason,
            model_wait_error_code=task.model_wait_error_code,
            model_wait_stage=task.model_wait_stage,
            model_wait_until=task.model_wait_until,
            model_wait_single_deadline_at=task.model_wait_single_deadline_at,
            model_wait_seconds_used=wait_seconds_used,
            model_wait_total_limit_seconds=task.model_wait_total_limit_seconds,
            auto_recovery_count=task.auto_recovery_count,
            auto_recovery_limit=task.auto_recovery_limit,
            auto_recovery_budget_exhausted=(
                None
                if task.auto_recovery_count is None or task.auto_recovery_limit is None
                else task.auto_recovery_count >= task.auto_recovery_limit
            ),
            summary_requests_used=summary_requests_used,
            summary_requests_limit=MAX_SUMMARY_REQUESTS,
            summary_requests_exhausted=summary_requests_used >= MAX_SUMMARY_REQUESTS,
            completed_segments=int(completed),
            expected_segments=int(expected),
            transcript_complete=(int(expected) > 0 and int(completed) == int(expected)),
            asr_model=str(attempt.config_snapshot["asr"]["model"]),
            config_sha256=attempt.config_sha256,
            source_fingerprint=attempt.source_fingerprint,
            manifest_fingerprint=attempt.manifest_fingerprint,
            asr_request_count=int(request_count),
            asr_elapsed_seconds=float(elapsed_seconds),
            reused_segments=int(reused_count),
            asr_calls=[
                {
                    "segment_number": row.segment_number,
                    **{
                        key: row.request_metadata[key]
                        for key in (
                            "model",
                            "http_status",
                            "trace_id",
                            "response_bytes",
                            "text_characters",
                            "elapsed_seconds",
                        )
                        if key in row.request_metadata
                    },
                }
                for row in call_rows
                if row.request_metadata is not None
            ],
            summary_status=task.summary_status,
            execution_state=(
                "not_started"
                if execution is None
                else ("stopped" if execution.stop_confirmed_at is not None else "stop_unconfirmed")
            ),
            execution_stop_confirmed=(None if execution is None else execution.stop_confirmed_at is not None),
            failure_code=task.failure_code,
            recovery_eligible=recovery_allowed,
            recovery_error_code=(
                "VIDEO_MEDIA_UNAVAILABLE"
                if video.media_status != "available"
                else
                "SUMMARY_REQUEST_BUDGET_EXHAUSTED"
                if eligibility.eligible and summary_requests_used >= MAX_SUMMARY_REQUESTS
                else "STOP_CONFIRMATION_REQUIRED"
                if eligibility.eligible and not stop_evidence_valid
                else "MANUAL_RECOVERY_NOT_RECOMMENDED"
                if eligibility.eligible and not recoverable_by_operator
                else eligibility.error_code
            ),
            recovery_available_at=eligibility.available_at,
            is_current_attempt=is_current,
            created_at=task.created_at,
            attempt=AnalysisAttemptResponse(
                id=attempt.id,
                attempt_number=attempt.attempt_number,
                parent_attempt_id=attempt.parent_attempt_id,
                status=attempt.status,
                execution_mode=attempt.execution_mode,
                execution_id=attempt.execution_id,
                first_claimed_at=attempt.first_claimed_at,
                stop_confirmed_at=attempt.stop_confirmed_at,
                cleanup_state=attempt.cleanup_state,
                cleanup_error_code=attempt.cleanup_error_code,
                failure_code=attempt.failure_code,
            ),
        )


def transcript_response(
    factory: sessionmaker[Session], owner_id: uuid.UUID, video_id: uuid.UUID
) -> TranscriptResponse:
    with factory() as session:
        video = session.scalar(select(Video).where(Video.id == video_id, Video.owner_id == owner_id))
        if video is None:
            raise HTTPException(status_code=404, detail="transcript not found")
        if video.status != "ready":
            raise HTTPException(
                status_code=409,
                detail={"code": "VIDEO_DELETION_IN_PROGRESS", "recovery_eligible": False},
            )
        task = session.scalar(
            select(AnalysisTask).where(
                AnalysisTask.video_id == video_id,
                AnalysisTask.owner_id == owner_id,
            )
        )
        if task is None or (task.notes_attempt_id is None and task.current_attempt_id is None):
            raise HTTPException(status_code=404, detail="transcript not found")
        selected_attempt_id = task.notes_attempt_id or task.current_attempt_id
        attempt = session.get(AnalysisAttempt, selected_attempt_id)
        if attempt is None:
            raise HTTPException(status_code=404, detail="transcript not found")
        segments = list(
            session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.attempt_id == attempt.id)
                .order_by(TranscriptSegment.segment_number)
            )
        )
        return TranscriptResponse(
            task_id=task.id,
            complete=(
                bool(segments)
                and attempt.manifest_fingerprint is not None
                and all(item.status == "succeeded" for item in segments)
            ),
            summary_status=task.summary_status,
            items=[
                TranscriptSegmentResponse(
                    segment_number=item.segment_number,
                    source_attempt_id=item.source_attempt_id,
                    audio_start_seconds=item.audio_start_seconds,
                    audio_end_seconds=item.audio_end_seconds,
                    actual_audio_seconds=item.actual_audio_seconds,
                    video_start_seconds=item.video_start_seconds,
                    video_end_seconds=item.video_end_seconds,
                    text=item.text,
                    status=item.status,
                )
                for item in segments
            ],
        )


def summary_response(
    factory: sessionmaker[Session], owner_id: uuid.UUID, video_id: uuid.UUID
) -> AnalysisSummaryResponse:
    with factory() as session:
        video = session.scalar(select(Video).where(Video.id == video_id, Video.owner_id == owner_id))
        if video is None:
            raise HTTPException(status_code=404, detail="summary not found")
        if video.status != "ready":
            raise HTTPException(
                status_code=409,
                detail={"code": "VIDEO_DELETION_IN_PROGRESS", "summary_available": False},
            )
        task = session.scalar(
            select(AnalysisTask).where(
                AnalysisTask.video_id == video.id,
                AnalysisTask.owner_id == owner_id,
            )
        )
        if task is None:
            raise HTTPException(status_code=404, detail="summary not found")
        if task.status != "succeeded" or task.summary_status != "succeeded":
            raise HTTPException(
                status_code=409,
                detail={"code": "SUMMARY_NOT_READY", "summary_status": task.summary_status},
            )
        result = session.scalar(
            select(AnalysisSummary).where(
                AnalysisSummary.task_id == task.id,
                AnalysisSummary.video_id == video.id,
            )
        )
        if result is None:
            raise HTTPException(status_code=503, detail="summary publication needs maintenance")
        return AnalysisSummaryResponse(
            video_id=result.video_id,
            task_id=result.task_id,
            attempt_id=result.attempt_id,
            status="succeeded",
            structure_version=result.structure_version,
            provider=result.provider,
            model=result.model,
            prompt_version=result.prompt_version,
            prompt_sha256=result.prompt_sha256,
            manifest_fingerprint=result.manifest_fingerprint,
            transcript_fingerprint=result.transcript_fingerprint,
            summary_sha256=result.payload_sha256,
            payload=result.payload,
            created_at=result.created_at,
        )
