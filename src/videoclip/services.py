from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import ClientDisconnect

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
                    deletion_mode=True,
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
        if cleanup_succeeded:
            try:
                store.remove_lease(upload_id)
            except OSError:
                logger.warning("lease_cleanup_pending upload_id=%s", upload_id.hex)


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
    deletion_mode: bool,
) -> bool:
    with db_transaction(factory) as session:
        video = session.scalar(
            select(Video).where(Video.id == video_id, Video.owner_id == owner_id).with_for_update()
        )
        if video is None:
            return False
        if deletion_mode:
            if video.status not in {"deleting", "cleanup_failed"}:
                return False
        elif video.status != "ready":
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

    try:
        store.remove_attempt_directory(task_id, work_directory_name)
    except OSError:
        with db_transaction(factory) as session:
            attempt = session.scalar(
                select(AnalysisAttempt).where(AnalysisAttempt.id == attempt_id).with_for_update()
            )
            if attempt is not None:
                attempt.cleanup_state = "failed"
                attempt.cleanup_error_code = "ATTEMPT_TEMP_CLEANUP_FAILED"
        return False
    with db_transaction(factory) as session:
        attempt = session.scalar(
            select(AnalysisAttempt).where(AnalysisAttempt.id == attempt_id).with_for_update()
        )
        if attempt is not None:
            attempt.cleanup_state = "cleaned"
            attempt.cleanup_error_code = None
    return True


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
        if intent is not None:
            intent.state = "cleanup_failed"
            intent.error_code = code
            intent.attempts += 1


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
    cleaned = _remove_attempt_files(factory, store, owner_id, video_id, attempt_id, deletion_mode=False)
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
                select(Video.owner_id, Video.id)
                .join(AnalysisTask, AnalysisTask.video_id == Video.id)
                .where(AnalysisTask.id == task_id)
            ).first()
            attempt = session.get(AnalysisAttempt, attempt_id)
            if row is None or attempt is None or not _attempt_stop_is_confirmed(session, attempt):
                continue
            owner_id, video_id = row
        try:
            _remove_attempt_files(
                factory,
                store,
                owner_id,
                video_id,
                attempt_id,
                deletion_mode=False,
            )
        except Exception:
            logger.warning("attempt_cleanup_recovery_pending attempt_id=%s", attempt_id.hex)
