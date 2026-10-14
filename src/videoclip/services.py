from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import unquote

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import ClientDisconnect

from videoclip.events import record_analysis_event
from videoclip.media import MediaError, MediaInfo, probe_media
from videoclip.models import (
    AnalysisAttempt,
    AnalysisExecution,
    AnalysisSummary,
    AnalysisTask,
    CleanupIntent,
    SummaryRequest,
    TaskDelivery,
    TranscriptSegment,
    UploadHandoff,
    User,
    Video,
)
from videoclip.security import hash_password, validate_username
from videoclip.settings import Settings
from videoclip.storage import MediaStore, StorageFailure, WriterLease

logger = logging.getLogger("videoclip.services")


@contextmanager
def db_transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        with session.begin():
            yield session


def register_account(factory: sessionmaker[Session], username: str, password: str) -> User:
    try:
        normalized = validate_username(username)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid username") from exc
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid password") from exc
    user = User(username=normalized, password_hash=password_hash)
    try:
        with db_transaction(factory) as session:
            session.add(user)
            session.flush()
        return user
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="username is unavailable") from exc


def safe_display_name(value: str | None) -> str:
    candidate = unquote(value or "video").strip().replace("\\", "/").rsplit("/", 1)[-1]
    candidate = "".join(
        character for character in candidate if ord(character) >= 32 and ord(character) != 127
    )
    candidate = candidate or "video"
    while len(candidate.encode("utf-8")) > 255:
        candidate = candidate[:-1]
    return candidate


def _intent_for_upload(
    session: Session, owner_id: uuid.UUID, upload_id: uuid.UUID, state: str
) -> CleanupIntent:
    intent = session.scalar(
        select(CleanupIntent)
        .where(CleanupIntent.upload_id == upload_id, CleanupIntent.kind == "upload")
        .with_for_update()
    )
    if intent is None:
        intent = CleanupIntent(
            owner_id=owner_id,
            upload_id=upload_id,
            storage_name=upload_id.hex,
            kind="upload",
            state=state,
            attempts=0,
        )
        session.add(intent)
    else:
        intent.state = state
        intent.attempts += 1
    return intent


def _mark_upload_failed(
    factory: sessionmaker[Session], upload_id: uuid.UUID, owner_id: uuid.UUID, code: str
) -> None:
    with db_transaction(factory) as session:
        upload = session.get(UploadHandoff, upload_id, with_for_update=True)
        if upload is None or upload.state == "complete":
            return
        upload.state = "cleanup_pending"
        upload.error_code = code[:64]
        _intent_for_upload(session, owner_id, upload_id, "cleanup_pending")


def _finish_failed_upload(
    factory: sessionmaker[Session], store: MediaStore, upload_id: uuid.UUID, lease: WriterLease
) -> bool:
    try:
        store.remove_part(upload_id)
        store.remove_media(upload_id.hex)
    except OSError:
        return False
    with db_transaction(factory) as session:
        session.execute(
            select(UploadHandoff).where(UploadHandoff.id == upload_id).with_for_update()
        ).scalar_one_or_none()
        session.query(CleanupIntent).filter(CleanupIntent.upload_id == upload_id).delete(
            synchronize_session=False
        )
        session.query(UploadHandoff).filter(UploadHandoff.id == upload_id).delete(synchronize_session=False)
    return True


# 把待验证文件的接管意图先写入数据库，失败后仍可由恢复任务继续处理。
def _create_handoff(
    factory: sessionmaker[Session],
    owner_id: uuid.UUID,
    upload_id: uuid.UUID,
    writer_id: uuid.UUID,
    display_name: str,
) -> None:
    with db_transaction(factory) as session:
        session.add(
            UploadHandoff(
                id=upload_id,
                owner_id=owner_id,
                writer_id=writer_id,
                part_name=f"{upload_id.hex}.part",
                target_name=upload_id.hex,
                display_name=display_name,
                state="receiving",
                byte_count=0,
            )
        )
        session.flush()
        _intent_for_upload(session, owner_id, upload_id, "writer_active")


def _set_upload_validating(
    factory: sessionmaker[Session], upload_id: uuid.UUID, byte_count: int, digest: str
) -> None:
    with db_transaction(factory) as session:
        upload = session.get(UploadHandoff, upload_id, with_for_update=True)
        if upload is None:
            raise RuntimeError("upload handoff disappeared")
        upload.state = "validating"
        upload.byte_count = byte_count
        upload.content_sha256 = digest
        _intent_for_upload(session, upload.owner_id, upload_id, "validating")


def _set_upload_promoting(factory: sessionmaker[Session], upload_id: uuid.UUID, media: MediaInfo) -> None:
    with db_transaction(factory) as session:
        upload = session.get(UploadHandoff, upload_id, with_for_update=True)
        if upload is None:
            raise RuntimeError("upload handoff disappeared")
        upload.state = "promoting"
        upload.duration_seconds = media.duration_seconds
        upload.media_info = media.as_dict()
        upload.error_code = None
        _intent_for_upload(session, upload.owner_id, upload_id, "promotion_pending")


# 只有媒体探测通过且正式文件已落盘后，事务才把视频标为可用。
def _complete_upload(factory: sessionmaker[Session], upload_id: uuid.UUID) -> Video:
    with db_transaction(factory) as session:
        upload = session.get(UploadHandoff, upload_id, with_for_update=True)
        if upload is None:
            raise RuntimeError("upload handoff disappeared")
        existing = session.scalar(select(Video).where(Video.upload_id == upload_id))
        if existing is not None:
            upload.state = "complete"
            session.query(CleanupIntent).filter(CleanupIntent.upload_id == upload_id).delete(
                synchronize_session=False
            )
            return existing
        if (
            upload.state != "promoting"
            or upload.content_sha256 is None
            or upload.media_info is None
            or upload.duration_seconds is None
        ):
            raise RuntimeError("upload handoff is not ready for registration")
        video = Video(
            owner_id=upload.owner_id,
            upload_id=upload.id,
            display_name=upload.display_name,
            storage_name=upload.target_name,
            byte_count=upload.byte_count,
            content_sha256=upload.content_sha256,
            duration_seconds=upload.duration_seconds,
            media_info=upload.media_info,
            status="ready",
        )
        session.add(video)
        upload.state = "complete"
        session.query(CleanupIntent).filter(CleanupIntent.upload_id == upload_id).delete(
            synchronize_session=False
        )
        session.flush()
        return video


def upload_response_data(factory: sessionmaker[Session], upload_id: uuid.UUID) -> Video:
    with factory() as session:
        video = session.scalar(select(Video).where(Video.upload_id == upload_id))
        if video is None:
            raise HTTPException(status_code=503, detail="upload finalization is pending recovery")
        return video


# 上传流先写临时文件并限制总字节数，校验摘要和媒体格式后才接管为正式视频。
async def receive_video_upload(
    request: Request,
    factory: sessionmaker[Session],
    settings: Settings,
    store: MediaStore,
    owner_id: uuid.UUID,
) -> Video:
    upload_id = uuid.uuid4()
    writer_id = uuid.uuid4()
    display_name = safe_display_name(request.headers.get("x-video-filename"))
    lease: WriterLease | None = None
    part_file: Any = None
    handoff_created = False
    finalization_attempted = False
    remove_lease_after_release = False
    try:
        lease = store.acquire_lease(upload_id)
        if lease is None:
            raise StorageFailure("upload lease is unexpectedly occupied")
        _create_handoff(factory, owner_id, upload_id, writer_id, display_name)
        handoff_created = True
        part_file = store.part_path(upload_id).open("xb", buffering=0)
        digest = hashlib.sha256()
        byte_count = 0
        deadline = time.monotonic() + settings.upload_total_timeout_seconds
        try:
            async with asyncio.timeout(settings.upload_total_timeout_seconds):
                async for chunk in request.stream():
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    if not chunk:
                        continue
                    digest.update(chunk)
                    byte_count += len(chunk)
                    store.write_chunk(part_file, chunk)
                store.flush_part(part_file)
        except TimeoutError as exc:
            raise HTTPException(status_code=408, detail="upload exceeded its total deadline") from exc
        part_file.close()
        part_file = None
        if byte_count == 0:
            raise HTTPException(status_code=422, detail="empty video uploads are not accepted")
        digest_value = digest.hexdigest()
        _set_upload_validating(factory, upload_id, byte_count, digest_value)
        remaining = deadline - time.monotonic() - settings.ffprobe_stop_grace_seconds * 3
        if remaining <= 0:
            raise HTTPException(status_code=408, detail="upload exceeded its total deadline")
        probe_timeout = min(float(settings.ffprobe_timeout_seconds), remaining)
        probe_task = asyncio.create_task(
            asyncio.to_thread(
                probe_media,
                store.part_path(upload_id),
                settings.ffprobe_path,
                probe_timeout,
                float(settings.ffprobe_stop_grace_seconds),
            )
        )
        try:
            media = await asyncio.shield(probe_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(probe_task)
            finally:
                raise
        except MediaError as exc:
            raise HTTPException(status_code=422, detail=exc.code.lower()) from exc
        _set_upload_promoting(factory, upload_id, media)
        store.promote(upload_id, upload_id.hex)
        finalization_attempted = True
        try:
            _complete_upload(factory, upload_id)
        except (SQLAlchemyError, OSError, RuntimeError) as exc:
            recovered = upload_response_data(factory, upload_id)
            if recovered is not None:
                return recovered
            raise HTTPException(status_code=503, detail="upload finalization is pending recovery") from exc
        return upload_response_data(factory, upload_id)
    except BaseException as exc:
        if handoff_created and not finalization_attempted:
            code = (
                exc.detail
                if isinstance(exc, HTTPException)
                else (exc.code if isinstance(exc, MediaError) else "UPLOAD_FAILED")
            )
            try:
                _mark_upload_failed(factory, upload_id, owner_id, str(code))
                if part_file is not None:
                    part_file.close()
                    part_file = None
                cleaned = _finish_failed_upload(factory, store, upload_id, lease) if lease else False
                if cleaned:
                    remove_lease_after_release = True
            except SQLAlchemyError:
                logger.warning("upload_cleanup_pending upload_id=%s", upload_id.hex)
            except OSError:
                logger.warning("upload_cleanup_pending upload_id=%s", upload_id.hex)
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, ClientDisconnect):
            raise
        logger.warning("upload_failed upload_id=%s", upload_id.hex)
        if isinstance(exc, MediaError):
            raise HTTPException(status_code=422, detail=exc.code.lower()) from exc
        if isinstance(exc, StorageFailure | OSError):
            raise HTTPException(
                status_code=507, detail="storage operation failed; cleanup is pending"
            ) from exc
        raise HTTPException(status_code=500, detail="upload failed; cleanup is pending") from exc
    finally:
        if part_file is not None:
            part_file.close()
        if lease is not None:
            lease.release()
            if not handoff_created or remove_lease_after_release:
                try:
                    store.remove_lease(upload_id)
                except OSError:
                    logger.warning("upload_lease_cleanup_pending upload_id=%s", upload_id.hex)


def _new_cleanup_intent(session: Session, video: Video) -> CleanupIntent:
    intent = session.scalar(
        select(CleanupIntent)
        .where(CleanupIntent.upload_id == video.upload_id, CleanupIntent.kind == "video")
        .with_for_update()
    )
    if intent is None:
        intent = CleanupIntent(
            owner_id=video.owner_id,
            upload_id=video.upload_id,
            storage_name=video.storage_name,
            kind="video",
            state="pending",
            attempts=0,
        )
        session.add(intent)
    return intent


def _new_media_cleanup_intent(session: Session, video: Video) -> CleanupIntent:
    intent = session.scalar(
        select(CleanupIntent)
        .where(CleanupIntent.upload_id == video.upload_id, CleanupIntent.kind == "media")
        .with_for_update()
    )
    if intent is None:
        intent = CleanupIntent(
            owner_id=video.owner_id,
            upload_id=video.upload_id,
            storage_name=video.storage_name,
            kind="media",
            state="pending",
            attempts=0,
        )
        session.add(intent)
    return intent


def _pin_notes_attempt(session: Session, task: AnalysisTask) -> None:
    if task.notes_attempt_id is not None:
        return
    published_attempt_id = session.scalar(
        select(AnalysisSummary.attempt_id).where(AnalysisSummary.task_id == task.id)
    )
    if published_attempt_id is not None:
        task.notes_attempt_id = published_attempt_id
        return

    versions = list(
        session.execute(
            select(
                AnalysisAttempt.id,
                AnalysisAttempt.attempt_number,
                func.count(TranscriptSegment.id).filter(TranscriptSegment.status == "succeeded"),
            )
            .outerjoin(TranscriptSegment, TranscriptSegment.attempt_id == AnalysisAttempt.id)
            .where(AnalysisAttempt.task_id == task.id)
            .group_by(AnalysisAttempt.id)
        ).all()
    )
    if not versions:
        task.notes_attempt_id = task.current_attempt_id
        return
    selected_id, _, _ = max(
        versions,
        key=lambda row: (
            int(row[2]),
            row[0] == task.current_attempt_id,
            row[1],
        ),
    )
    task.notes_attempt_id = selected_id


def _cancel_unfinished_analysis_for_media(session: Session, video: Video, now: datetime) -> None:
    task_id = video.current_analysis_task_id
    if task_id is None:
        if video.analysis_task_created_at is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "ANALYSIS_TASK_ASSOCIATION_MISSING", "deletion_eligible": False},
            )
        return
    task = session.scalar(select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update())
    if task is None or task.video_id != video.id:
        raise HTTPException(
            status_code=409,
            detail={"code": "ANALYSIS_TASK_ASSOCIATION_MISSING", "deletion_eligible": False},
        )
    _pin_notes_attempt(session, task)
    attempts = list(
        session.scalars(
            select(AnalysisAttempt)
            .where(AnalysisAttempt.task_id == task.id)
            .order_by(AnalysisAttempt.attempt_number)
            .with_for_update()
        )
    )
    attempt_ids = [attempt.id for attempt in attempts]
    deliveries = list(
        session.scalars(
            select(TaskDelivery)
            .where(TaskDelivery.attempt_id.in_(attempt_ids))
            .order_by(TaskDelivery.attempt_id)
            .with_for_update()
        )
    ) if attempt_ids else []
    has_published_summary = session.scalar(
        select(AnalysisSummary.id).where(AnalysisSummary.task_id == task.id).limit(1)
    ) is not None

    task.queue_entered_at = None
    task.delivery_next_check_at = None
    task.delivery_blocked_since = None
    task.delivery_wait_reason = "VIDEO_MEDIA_DELETION_REQUESTED"
    task.model_wait_reason = None
    task.model_wait_error_code = None
    task.model_wait_stage = None
    task.model_wait_origin = None
    task.model_wait_until = None
    task.model_wait_single_deadline_at = None

    if not has_published_summary:
        task.status = "failed"
        task.stage = "failed"
        task.failure_code = "VIDEO_MEDIA_DELETION_REQUESTED"
        if task.summary_status in {"waiting", "running"}:
            task.summary_status = "failed"

    for attempt in attempts:
        if attempt.status not in {"failed", "succeeded"}:
            attempt.status = "failed"
            attempt.failure_code = "VIDEO_MEDIA_DELETION_REQUESTED"
            attempt.finished_at = attempt.finished_at or now

    for delivery in deliveries:
        delivery.status = "failed"
        delivery.error_code = "VIDEO_MEDIA_DELETION_REQUESTED"
        delivery.operation_generation = (delivery.operation_generation or 0) + 1
        delivery.updated_at = now
    unfinished_requests = list(
        session.scalars(
            select(SummaryRequest)
            .where(
                SummaryRequest.task_id == task.id,
                SummaryRequest.status.in_(("started", "response_received")),
            )
            .order_by(SummaryRequest.id)
            .with_for_update()
        )
    )
    for request in unfinished_requests:
        request.status = "failed"
        request.error_code = "VIDEO_MEDIA_DELETION_REQUESTED"
        request.finished_at = now


def _resume_media_cleanup(
    factory: sessionmaker[Session],
    store: MediaStore,
    video_id: uuid.UUID,
    *,
    owner_id: uuid.UUID | None = None,
) -> bool:
    with factory() as session:
        query = select(Video).where(Video.id == video_id)
        if owner_id is not None:
            query = query.where(Video.owner_id == owner_id)
        video = session.scalar(query)
        if video is None:
            return False
        if video.media_status == "deleted":
            return True
        if video.media_status not in {"deletion_pending", "cleanup_failed"}:
            return False
        task = session.scalar(select(AnalysisTask).where(AnalysisTask.video_id == video.id))
        attempts = (
            list(
                session.scalars(
                    select(AnalysisAttempt)
                    .where(AnalysisAttempt.task_id == task.id)
                    .order_by(AnalysisAttempt.attempt_number)
                )
            )
            if task is not None
            else []
        )
        if any(not _attempt_stop_is_confirmed(session, attempt) for attempt in attempts):
            return False
        upload_id = video.upload_id

    try:
        lease = store.acquire_lease(upload_id)
    except OSError:
        _set_media_cleanup_failure(factory, video_id, upload_id, "FILE_LEASE_UNAVAILABLE")
        return False
    if lease is None:
        return False

    try:
        with db_transaction(factory) as session:
            current = session.scalar(select(Video).where(Video.id == video_id).with_for_update())
            if current is None:
                return False
            if current.media_status == "deleted":
                return True
            if current.media_status not in {"deletion_pending", "cleanup_failed"}:
                return False
            task = session.scalar(
                select(AnalysisTask).where(AnalysisTask.video_id == current.id).with_for_update()
            )
            attempts = (
                list(
                    session.scalars(
                        select(AnalysisAttempt)
                        .where(AnalysisAttempt.task_id == task.id)
                        .order_by(AnalysisAttempt.attempt_number)
                        .with_for_update()
                    )
                )
                if task is not None
                else []
            )
            if any(not _attempt_stop_is_confirmed(session, attempt) for attempt in attempts):
                return False
            task_id = task.id if task is not None else None
            cleanup_rows = [(attempt.id, attempt.work_directory_name) for attempt in attempts]
            storage_name = current.storage_name
            upload_id = current.upload_id
            owner_id = current.owner_id

        if task_id is not None:
            for attempt_id, _ in cleanup_rows:
                if not _remove_attempt_files(
                    factory,
                    store,
                    owner_id,
                    video_id,
                    attempt_id,
                    cleanup_mode="media",
                ):
                    _set_media_cleanup_failure(
                        factory, video_id, upload_id, "ATTEMPT_TEMP_CLEANUP_FAILED"
                    )
                    return False
        store.remove_media(storage_name)
        with db_transaction(factory) as session:
            current = session.scalar(select(Video).where(Video.id == video_id).with_for_update())
            if current is None:
                return False
            if current.media_status != "deleted":
                current.media_status = "deleted"
                current.media_error_code = None
            intent = session.scalar(
                select(CleanupIntent)
                .where(CleanupIntent.upload_id == current.upload_id, CleanupIntent.kind == "media")
                .with_for_update()
            )
            if intent is not None:
                intent.state = "completed"
                intent.error_code = None
            task = session.scalar(select(AnalysisTask).where(AnalysisTask.video_id == video_id))
            if task is not None:
                record_analysis_event(
                    session,
                    task_id=task.id,
                    event_key=f"media-cleanup-completed:{uuid.uuid4()}",
                    event_type="media_cleanup_completed",
                    attempt_id=task.current_attempt_id,
                    stage=task.stage,
                )
        return True
    except OSError:
        _set_media_cleanup_failure(factory, video_id, upload_id, "STORAGE_OPERATION_FAILED")
        return False
    finally:
        lease.release()


# 先持久化媒体删除意图并取消未完成分析，再在事务外等待停止确认和清理文件。
def delete_video_media(
    factory: sessionmaker[Session], store: MediaStore, owner_id: uuid.UUID, video_id: uuid.UUID
) -> bool:
    with db_transaction(factory) as session:
        video = session.scalar(
            select(Video).where(Video.id == video_id, Video.owner_id == owner_id).with_for_update()
        )
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        if video.status != "ready":
            raise HTTPException(
                status_code=409,
                detail={"code": "VIDEO_DELETION_IN_PROGRESS", "deletion_eligible": False},
            )
        # 先持久化删除意图并取消未完成分析；磁盘清理在数据库事务提交后执行。
        if video.media_status == "available":
            video.media_status = "deletion_pending"
            video.media_error_code = None
            _new_media_cleanup_intent(session, video)
            _cancel_unfinished_analysis_for_media(session, video, datetime.now(UTC))
        elif video.media_status == "cleanup_failed":
            video.media_status = "deletion_pending"
            intent = _new_media_cleanup_intent(session, video)
            intent.state = "pending"
        elif video.media_status == "deletion_pending":
            _new_media_cleanup_intent(session, video)
        elif video.media_status == "deleted":
            return True
        else:
            raise HTTPException(status_code=503, detail="video media state needs maintenance")
        task_id = video.current_analysis_task_id
        if task_id is not None:
            task = session.get(AnalysisTask, task_id)
            record_analysis_event(
                session,
                task_id=task_id,
                event_key=f"media-cleanup-requested:{uuid.uuid4()}",
                event_type="media_cleanup_started",
                attempt_id=task.current_attempt_id if task is not None else None,
                stage=task.stage if task is not None else None,
                actor="user",
            )
    return _resume_media_cleanup(factory, store, video_id, owner_id=owner_id)


def retry_video_media_cleanup(
    factory: sessionmaker[Session], store: MediaStore, owner_id: uuid.UUID, video_id: uuid.UUID
) -> bool:
    with db_transaction(factory) as session:
        video = session.scalar(
            select(Video).where(Video.id == video_id, Video.owner_id == owner_id).with_for_update()
        )
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        if video.status != "ready":
            raise HTTPException(
                status_code=409,
                detail={"code": "VIDEO_DELETION_IN_PROGRESS", "deletion_eligible": False},
            )
        if video.media_status == "deleted":
            return True
        if video.media_status not in {"deletion_pending", "cleanup_failed"}:
            raise HTTPException(
                status_code=409,
                detail={"code": "MEDIA_DELETION_NOT_REQUESTED", "cleanup_eligible": False},
            )
        video.media_status = "deletion_pending"
        _new_media_cleanup_intent(session, video).state = "pending"
        task = session.scalar(select(AnalysisTask).where(AnalysisTask.video_id == video.id))
        if task is not None:
            record_analysis_event(
                session,
                task_id=task.id,
                event_key=f"media-cleanup-retry:{uuid.uuid4()}",
                event_type="media_cleanup_started",
                attempt_id=task.current_attempt_id,
                stage=task.stage,
                actor="user",
            )
    return _resume_media_cleanup(factory, store, video_id, owner_id=owner_id)


def recover_pending_media_deletions(
    factory: sessionmaker[Session], store: MediaStore, *, limit: int = 4
) -> int:
    if not 1 <= limit <= 8:
        raise ValueError("media deletion recovery limit must be between 1 and 8")
    with factory() as session:
        candidates = list(
            session.scalars(
                select(Video.id)
                .where(
                    Video.status == "ready",
                    Video.media_status.in_(("deletion_pending", "cleanup_failed")),
                )
                .order_by(Video.created_at, Video.id)
                .limit(limit)
            )
        )
    completed = 0
    for video_id in candidates:
        try:
            completed += _resume_media_cleanup(factory, store, video_id)
        except Exception:
            logger.warning("media_cleanup_recovery_pending video_id=%s", video_id.hex)
    return completed


# 删除视频记录前先检查活动尝试与停止证明，防止仍运行的进程继续访问源文件。
def delete_video(
    factory: sessionmaker[Session], store: MediaStore, owner_id: uuid.UUID, video_id: uuid.UUID
) -> int:
    now = datetime.now(UTC)
    with db_transaction(factory) as session:
        video = session.scalar(
            select(Video).where(Video.id == video_id, Video.owner_id == owner_id).with_for_update()
        )
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        if video.analysis_task_created_at is not None and video.current_analysis_task_id is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "ANALYSIS_TASK_ASSOCIATION_MISSING", "deletion_eligible": False},
            )
        if video.status not in {"deleting", "cleanup_failed"}:
            video.status = "deleting"
        else:
            video.status = "deleting"
        if video.media_status not in {"deleted", "cleanup_failed"}:
            video.media_status = "deletion_pending"
        video.deletion_error = None
        intent = _new_cleanup_intent(session, video)
        intent.state = "pending"
        intent.error_code = None
        task_id = video.current_analysis_task_id
        upload_id = video.upload_id
        storage_name = video.storage_name
        attempts: list[tuple[uuid.UUID, str]] = []
        if task_id is not None:
            task = session.scalar(select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update())
            if task is not None:
                task.status = "failed"
                task.stage = "failed"
                task.failure_code = "VIDEO_DELETION_REQUESTED"
                record_analysis_event(
                    session,
                    task_id=task.id,
                    event_key=f"record-cleanup-requested:{uuid.uuid4()}",
                    event_type="record_cleanup_started",
                    attempt_id=task.current_attempt_id,
                    stage="failed",
                    actor="user",
                )
                task.queue_entered_at = None
                task.delivery_next_check_at = None
                task.delivery_blocked_since = None
                task.delivery_wait_reason = None
                task_attempts = list(
                    session.scalars(
                        select(AnalysisAttempt)
                        .where(AnalysisAttempt.task_id == task.id)
                        .order_by(AnalysisAttempt.attempt_number)
                        .with_for_update()
                    )
                )
                for attempt in task_attempts:
                    if attempt.status != "failed":
                        attempt.status = "failed"
                        attempt.failure_code = "VIDEO_DELETION_REQUESTED"
                    attempt.finished_at = attempt.finished_at or now
                    attempts.append((attempt.id, attempt.work_directory_name))

    if task_id is not None:
        with factory() as session:
            attempt_rows = list(
                session.scalars(
                    select(AnalysisAttempt)
                    .where(AnalysisAttempt.task_id == task_id)
                    .order_by(AnalysisAttempt.attempt_number)
                )
            )
            for attempt in attempt_rows:
                if not _attempt_stop_is_confirmed(session, attempt):
                    return status.HTTP_202_ACCEPTED

    lease = store.acquire_lease(upload_id)
    if lease is None:
        return status.HTTP_202_ACCEPTED
    cleanup_succeeded = False
    try:
        try:
            for attempt_id, _ in attempts:
                if not _remove_attempt_files(
                    factory,
                    store,
                    owner_id,
                    video_id,
                    attempt_id,
                    cleanup_mode="whole",
                ):
                    _set_video_cleanup_failure(factory, video_id, upload_id, "ATTEMPT_TEMP_CLEANUP_FAILED")
                    return status.HTTP_202_ACCEPTED
            store.remove_media(storage_name)
            cleanup_succeeded = True
        except OSError:
            _set_video_cleanup_failure(factory, video_id, upload_id, "STORAGE_OPERATION_FAILED")
            return status.HTTP_202_ACCEPTED
        if cleanup_succeeded:
            with db_transaction(factory) as session:
                current = session.scalar(select(Video).where(Video.id == video_id).with_for_update())
                if current is None:
                    return status.HTTP_204_NO_CONTENT
                current_task_id = current.current_analysis_task_id
                current.current_analysis_task_id = None
                if current_task_id is not None:
                    task = session.scalar(
                        select(AnalysisTask).where(AnalysisTask.id == current_task_id).with_for_update()
                    )
                    if task is not None:
                        task.current_attempt_id = None
                        task.notes_attempt_id = None
                        session.flush()
                        current_attempt_ids = list(
                            session.scalars(
                                select(AnalysisAttempt.id).where(AnalysisAttempt.task_id == task.id)
                            )
                        )
                        if current_attempt_ids:
                            session.query(SummaryRequest).filter(
                                SummaryRequest.attempt_id.in_(current_attempt_ids)
                            ).delete(synchronize_session=False)
                            session.query(AnalysisSummary).filter(AnalysisSummary.task_id == task.id).delete(
                                synchronize_session=False
                            )
                            session.query(TranscriptSegment).filter(
                                TranscriptSegment.attempt_id.in_(current_attempt_ids)
                            ).delete(synchronize_session=False)
                            session.query(TaskDelivery).filter(
                                TaskDelivery.attempt_id.in_(current_attempt_ids)
                            ).delete(synchronize_session=False)
                            session.query(AnalysisExecution).filter(
                                AnalysisExecution.attempt_id.in_(current_attempt_ids)
                            ).delete(synchronize_session=False)
                            session.query(AnalysisAttempt).filter(
                                AnalysisAttempt.id.in_(current_attempt_ids)
                            ).delete(synchronize_session=False)
                        session.delete(task)
                session.query(CleanupIntent).filter(CleanupIntent.upload_id == current.upload_id).delete(
                    synchronize_session=False
                )
                session.delete(current)
                session.flush()
                upload = session.get(UploadHandoff, current.upload_id, with_for_update=True)
                if upload is not None:
                    session.delete(upload)
        return status.HTTP_204_NO_CONTENT
    finally:
        lease.release()


def retry_cleanup(
    factory: sessionmaker[Session], store: MediaStore, owner_id: uuid.UUID, video_id: uuid.UUID
) -> int:
    return delete_video(factory, store, owner_id, video_id)


def _attempt_stop_is_confirmed(session: Session, attempt: AnalysisAttempt) -> bool:
    if attempt.claim_count == 0:
        execution_id = session.scalar(
            select(AnalysisExecution.id).where(AnalysisExecution.attempt_id == attempt.id)
        )
        return execution_id is None and attempt.status == "failed"
    if attempt.claim_count != 1 or attempt.execution_id is None or attempt.stop_confirmed_at is None:
        return False
    execution = session.scalar(select(AnalysisExecution).where(AnalysisExecution.attempt_id == attempt.id))
    return (
        execution is not None
        and execution.id == attempt.execution_id
        and execution.stop_confirmed_at is not None
    )


def _remove_attempt_files(
    factory: sessionmaker[Session],
    store: MediaStore,
    owner_id: uuid.UUID,
    video_id: uuid.UUID,
    attempt_id: uuid.UUID,
    *,
    cleanup_mode: Literal["normal", "media", "whole"],
) -> bool:
    with db_transaction(factory) as session:
        video = session.scalar(
            select(Video).where(Video.id == video_id, Video.owner_id == owner_id).with_for_update()
        )
        if video is None:
            return False
        if cleanup_mode == "whole":
            if video.status not in {"deleting", "cleanup_failed"}:
                return False
        elif cleanup_mode == "media":
            if (
                video.status not in {"ready", "deleting", "cleanup_failed"}
                or video.media_status not in {"deletion_pending", "cleanup_failed", "deleted"}
            ):
                return False
        elif video.status != "ready" or video.media_status != "available":
            return False
        task = session.scalar(select(AnalysisTask).where(AnalysisTask.video_id == video_id).with_for_update())
        if task is None:
            return False
        attempt = session.scalar(
            select(AnalysisAttempt)
            .where(AnalysisAttempt.id == attempt_id, AnalysisAttempt.task_id == task.id)
            .with_for_update()
        )
        if attempt is None or not _attempt_stop_is_confirmed(session, attempt):
            return False
        if attempt.cleanup_state == "cleaned":
            return True
        work_directory_name = attempt.work_directory_name
        task_id = task.id
        record_analysis_event(
            session,
            task_id=task.id,
            event_key=f"attempt-cleanup-started:{attempt.id}:{uuid.uuid4()}",
            event_type="attempt_cleanup_started",
            attempt_id=attempt.id,
            stage=task.stage,
        )

    try:
        store.remove_attempt_directory(task_id, work_directory_name)
    except OSError:
        with db_transaction(factory) as session:
            attempt_task_id = session.scalar(
                select(AnalysisAttempt.task_id).where(AnalysisAttempt.id == attempt_id)
            )
            if attempt_task_id is None:
                return False
            task = session.scalar(
                select(AnalysisTask).where(AnalysisTask.id == attempt_task_id).with_for_update()
            )
            attempt = session.scalar(
                select(AnalysisAttempt).where(AnalysisAttempt.id == attempt_id).with_for_update()
            )
            if attempt is not None and task is not None:
                attempt.cleanup_state = "failed"
                attempt.cleanup_error_code = "ATTEMPT_TEMP_CLEANUP_FAILED"
                record_analysis_event(
                    session,
                    task_id=task.id,
                    event_key=f"attempt-cleanup-failed:{attempt.id}:{uuid.uuid4()}",
                    event_type="attempt_cleanup_failed",
                    attempt_id=attempt.id,
                    stage="failed",
                    error_code=attempt.cleanup_error_code,
                )
        return False
    with db_transaction(factory) as session:
        attempt_task_id = session.scalar(
            select(AnalysisAttempt.task_id).where(AnalysisAttempt.id == attempt_id)
        )
        if attempt_task_id is None:
            return False
        task = session.scalar(
            select(AnalysisTask).where(AnalysisTask.id == attempt_task_id).with_for_update()
        )
        attempt = session.scalar(
            select(AnalysisAttempt).where(AnalysisAttempt.id == attempt_id).with_for_update()
        )
        if attempt is not None and task is not None:
            attempt.cleanup_state = "cleaned"
            attempt.cleanup_error_code = None
            record_analysis_event(
                session,
                task_id=task.id,
                event_key=f"attempt-cleanup-completed:{attempt.id}:{uuid.uuid4()}",
                event_type="attempt_cleanup_completed",
                attempt_id=attempt.id,
                stage="succeeded",
            )
    return True


def cleanup_confirmed_attempt_files(
    factory: sessionmaker[Session], store: MediaStore, attempt_id: uuid.UUID
) -> bool:
    """Clean only the exact attempt whose execution and attempt both carry stop confirmation."""
    with factory() as session:
        row = session.execute(
            select(Video.owner_id, Video.id, Video.status, Video.media_status)
            .join(AnalysisTask, AnalysisTask.video_id == Video.id)
            .join(AnalysisAttempt, AnalysisAttempt.task_id == AnalysisTask.id)
            .where(AnalysisAttempt.id == attempt_id)
        ).one_or_none()
        attempt = session.get(AnalysisAttempt, attempt_id)
        eligible = attempt is not None and _attempt_stop_is_confirmed(session, attempt)
        session.commit()
    if row is None or not eligible:
        return False
    owner_id, video_id, video_status, media_status = row
    if video_status == "ready":
        if media_status in {"deletion_pending", "cleanup_failed", "deleted"}:
            return _resume_media_cleanup(factory, store, video_id, owner_id=owner_id)
        cleanup_mode: Literal["normal", "media", "whole"] = "normal"
    elif video_status in {"deleting", "cleanup_failed"}:
        cleanup_mode = "whole"
    else:
        return False
    return _remove_attempt_files(
        factory,
        store,
        owner_id,
        video_id,
        attempt_id,
        cleanup_mode=cleanup_mode,
    )


def _set_video_cleanup_failure(
    factory: sessionmaker[Session], video_id: uuid.UUID, upload_id: uuid.UUID, code: str
) -> None:
    with db_transaction(factory) as session:
        video = session.scalar(select(Video).where(Video.id == video_id).with_for_update())
        intent = session.scalar(
            select(CleanupIntent)
            .where(CleanupIntent.upload_id == upload_id, CleanupIntent.kind == "video")
            .with_for_update()
        )
        if video is not None:
            video.status = "cleanup_failed"
            video.deletion_error = code
            task = session.scalar(select(AnalysisTask).where(AnalysisTask.video_id == video.id))
            if task is not None:
                record_analysis_event(
                    session,
                    task_id=task.id,
                    event_key=f"record-cleanup-failed:{uuid.uuid4()}",
                    event_type="record_cleanup_failed",
                    attempt_id=task.current_attempt_id,
                    stage="failed",
                    error_code=code,
                )
        if intent is not None:
            intent.state = "cleanup_failed"
            intent.error_code = code
            intent.attempts += 1


def _set_media_cleanup_failure(
    factory: sessionmaker[Session], video_id: uuid.UUID, upload_id: uuid.UUID, code: str
) -> None:
    with db_transaction(factory) as session:
        video = session.scalar(select(Video).where(Video.id == video_id).with_for_update())
        intent = session.scalar(
            select(CleanupIntent)
            .where(CleanupIntent.upload_id == upload_id, CleanupIntent.kind == "media")
            .with_for_update()
        )
        if video is not None and video.media_status != "deleted":
            video.media_status = "cleanup_failed"
            video.media_error_code = code
            task = session.scalar(select(AnalysisTask).where(AnalysisTask.video_id == video.id))
            if task is not None:
                record_analysis_event(
                    session,
                    task_id=task.id,
                    event_key=f"media-cleanup-failed:{uuid.uuid4()}",
                    event_type="media_cleanup_failed",
                    attempt_id=task.current_attempt_id,
                    stage="failed",
                    error_code=code,
                )
        if intent is not None:
            intent.state = "cleanup_failed"
            intent.error_code = code
            intent.attempts += 1


# 只清理已有可信停止确认的尝试文件；失败时保留意图和错误码供后续重试。
def retry_attempt_cleanup(
    factory: sessionmaker[Session],
    store: MediaStore,
    owner_id: uuid.UUID,
    video_id: uuid.UUID,
    attempt_id: uuid.UUID,
) -> tuple[str, str | None]:
    with factory() as session:
        video = session.scalar(select(Video).where(Video.id == video_id, Video.owner_id == owner_id))
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        if video.status != "ready":
            raise HTTPException(
                status_code=409,
                detail={"code": "VIDEO_DELETION_IN_PROGRESS", "recovery_eligible": False},
            )
        if video.media_status != "available":
            raise HTTPException(
                status_code=409,
                detail={"code": "VIDEO_MEDIA_UNAVAILABLE", "recovery_eligible": False},
            )
        task = session.scalar(
            select(AnalysisTask).where(
                AnalysisTask.id == video.current_analysis_task_id,
                AnalysisTask.video_id == video.id,
                AnalysisTask.owner_id == owner_id,
            )
        )
        attempt = session.get(AnalysisAttempt, attempt_id)
        if task is None or attempt is None or attempt.task_id != task.id:
            raise HTTPException(status_code=404, detail="analysis attempt not found")
        if not _attempt_stop_is_confirmed(session, attempt):
            raise HTTPException(
                status_code=409,
                detail={"code": "STOP_CONFIRMATION_REQUIRED", "cleanup_eligible": False},
            )
    cleaned = _remove_attempt_files(factory, store, owner_id, video_id, attempt_id, cleanup_mode="normal")
    if not cleaned:
        with factory() as session:
            attempt = session.get(AnalysisAttempt, attempt_id)
            if attempt is not None and attempt.cleanup_state == "failed":
                return attempt.cleanup_state, attempt.cleanup_error_code
        raise HTTPException(
            status_code=409,
            detail={"code": "ATTEMPT_CLEANUP_NOT_ELIGIBLE", "cleanup_eligible": False},
        )
    return "cleaned", None


def recover_confirmed_attempt_cleanup(factory: sessionmaker[Session], store: MediaStore) -> None:
    with factory() as session:
        candidates = list(
            session.execute(
                select(AnalysisAttempt.task_id, AnalysisAttempt.id)
                .join(AnalysisTask, AnalysisTask.id == AnalysisAttempt.task_id)
                .join(Video, Video.id == AnalysisTask.video_id)
                .where(
                    Video.status == "ready",
                    AnalysisAttempt.status.in_(("failed", "awaiting_summary", "succeeded")),
                    AnalysisAttempt.cleanup_state.in_(("pending", "failed")),
                )
                .order_by(AnalysisAttempt.created_at)
            ).all()
        )
    for task_id, attempt_id in candidates:
        with factory() as session:
            row = session.execute(
                select(Video.owner_id, Video.id, Video.media_status)
                .join(AnalysisTask, AnalysisTask.video_id == Video.id)
                .where(AnalysisTask.id == task_id)
            ).first()
            attempt = session.get(AnalysisAttempt, attempt_id)
            if row is None or attempt is None or not _attempt_stop_is_confirmed(session, attempt):
                continue
            owner_id, video_id, media_status = row
        try:
            if media_status in {"deletion_pending", "cleanup_failed", "deleted"}:
                _resume_media_cleanup(factory, store, video_id, owner_id=owner_id)
            else:
                _remove_attempt_files(
                    factory,
                    store,
                    owner_id,
                    video_id,
                    attempt_id,
                    cleanup_mode="normal",
                )
        except Exception:
            logger.warning("attempt_cleanup_recovery_pending attempt_id=%s", attempt_id.hex)
