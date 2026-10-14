from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import ClientDisconnect

from videoclip.media import MediaError, probe_media
from videoclip.models import CleanupIntent, UploadChunk, UploadHandoff, Video
from videoclip.services import (
    _complete_upload,
    _intent_for_upload,
    _set_upload_promoting,
    db_transaction,
    safe_display_name,
)
from videoclip.settings import Settings
from videoclip.storage import RESUMABLE_CHUNK_SIZE, MediaStore, StorageFailure, WriterLease

logger = logging.getLogger("videoclip.resumable_uploads")

CHUNK_SIZE = RESUMABLE_CHUNK_SIZE
UPLOAD_TTL = timedelta(hours=24)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[\x21-\x7e]{1,128}$")
_BIGINT_MAX = 9_223_372_036_854_775_807


class UploadChunkSummaryData(TypedDict):
    chunk_index: int
    byte_offset: int
    byte_count: int
    sha256: str


class UploadStatusData(TypedDict):
    upload_id: uuid.UUID
    state: str
    chunk_size: int
    total_bytes: int
    confirmed_offset: int
    next_chunk_index: int
    expires_at: datetime
    chunks: list[UploadChunkSummaryData]
    error_code: str | None
    video_id: uuid.UUID | None


class UploadChunkResponseData(TypedDict):
    upload_id: uuid.UUID
    state: str
    chunk_index: int
    byte_offset: int
    byte_count: int
    sha256: str
    confirmed_offset: int
    expires_at: datetime
    replayed: bool


def _error(status_code: int, code: str, **context: object) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, **context})


def _validate_idempotency_key(value: str) -> None:
    if not _IDEMPOTENCY_RE.fullmatch(value):
        raise _error(422, "INVALID_UPLOAD_METADATA")


def _validate_upload_metadata(total_bytes: int, sha256: str, idempotency_key: str) -> None:
    if total_bytes <= 0 or total_bytes > _BIGINT_MAX or not _SHA256_RE.fullmatch(sha256):
        raise _error(422, "INVALID_UPLOAD_METADATA")
    _validate_idempotency_key(idempotency_key)


def _matching_create_request(
    upload: UploadHandoff, display_name: str, total_bytes: int, sha256: str
) -> bool:
    return (
        upload.protocol_type == "resumable"
        and upload.display_name == display_name
        and upload.declared_total_bytes == total_bytes
        and upload.expected_content_sha256 == sha256
    )


def create_upload_session(
    factory: sessionmaker[Session],
    owner_id: uuid.UUID,
    file_name: str,
    total_bytes: int,
    sha256: str,
    idempotency_key: str,
) -> tuple[uuid.UUID, str, int, int, datetime, bool]:
    _validate_upload_metadata(total_bytes, sha256, idempotency_key)
    display_name = safe_display_name(file_name)
    now = datetime.now(UTC)

    def find_existing() -> UploadHandoff | None:
        with factory() as session:
            return session.scalar(
                select(UploadHandoff).where(
                    UploadHandoff.owner_id == owner_id,
                    UploadHandoff.idempotency_key == idempotency_key,
                )
            )

    try:
        with db_transaction(factory) as session:
            existing = session.scalar(
                select(UploadHandoff)
                .where(
                    UploadHandoff.owner_id == owner_id,
                    UploadHandoff.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            if existing is not None:
                if not _matching_create_request(existing, display_name, total_bytes, sha256):
                    raise _error(409, "UPLOAD_IDEMPOTENCY_CONFLICT")
                if existing.expires_at is None:
                    raise _error(500, "UPLOAD_STORAGE_CORRUPT")
                return (
                    existing.id,
                    existing.state,
                    existing.declared_total_bytes or total_bytes,
                    existing.confirmed_offset,
                    existing.expires_at,
                    True,
                )
            upload_id = uuid.uuid4()
            expires_at = now + UPLOAD_TTL
            upload = UploadHandoff(
                id=upload_id,
                owner_id=owner_id,
                writer_id=uuid.uuid4(),
                part_name=f"{upload_id.hex}.part",
                target_name=upload_id.hex,
                display_name=display_name,
                state="receiving",
                byte_count=0,
                protocol_type="resumable",
                declared_total_bytes=total_bytes,
                expected_content_sha256=sha256,
                confirmed_offset=0,
                expires_at=expires_at,
                idempotency_key=idempotency_key,
            )
            session.add(upload)
            session.flush()
            return upload_id, upload.state, total_bytes, 0, expires_at, False
    except IntegrityError:
        existing = find_existing()
        if existing is None or not _matching_create_request(existing, display_name, total_bytes, sha256):
            raise _error(409, "UPLOAD_IDEMPOTENCY_CONFLICT") from None
        if existing.expires_at is None:
            raise _error(500, "UPLOAD_STORAGE_CORRUPT") from None
        return (
            existing.id,
            existing.state,
            existing.declared_total_bytes or total_bytes,
            existing.confirmed_offset,
            existing.expires_at,
            True,
        )


def _acquire_lease(store: MediaStore, upload_id: uuid.UUID) -> WriterLease:
    try:
        lease = store.acquire_lease(upload_id)
    except OSError as exc:
        raise _error(507, "UPLOAD_STORAGE_FAILED") from exc
    if lease is None:
        raise _error(409, "UPLOAD_BUSY")
    return lease


def _load_owned_upload(
    factory: sessionmaker[Session], upload_id: uuid.UUID, owner_id: uuid.UUID, *, lock: bool = False
) -> UploadHandoff:
    with factory() as session:
        statement = select(UploadHandoff).where(
            UploadHandoff.id == upload_id,
            UploadHandoff.owner_id == owner_id,
            UploadHandoff.protocol_type == "resumable",
        )
        if lock:
            statement = statement.with_for_update()
        upload = session.scalar(statement)
        if upload is None:
            raise _error(404, "UPLOAD_NOT_FOUND")
        return upload


def _load_chunks(factory: sessionmaker[Session], upload_id: uuid.UUID) -> list[UploadChunk]:
    with factory() as session:
        return list(
            session.scalars(
                select(UploadChunk)
                .where(UploadChunk.upload_id == upload_id)
                .order_by(UploadChunk.chunk_index)
            )
        )


def _chunk_manifest(chunks: list[UploadChunk]) -> list[tuple[int, int, int, str]]:
    return [
        (chunk.chunk_index, chunk.byte_offset, chunk.byte_count, chunk.content_sha256)
        for chunk in chunks
    ]


def _now() -> datetime:
    return datetime.now(UTC)


def _check_mutable_state(upload: UploadHandoff, now: datetime) -> None:
    if upload.state == "expired" or (
        upload.state in {"receiving", "validating"}
        and upload.expires_at is not None
        and upload.expires_at <= now
    ):
        raise _error(410, "UPLOAD_EXPIRED")
    if upload.state == "abandoned":
        raise _error(410, "UPLOAD_ABANDONED")
    if upload.state == "failed":
        code = upload.error_code or "UPLOAD_STORAGE_CORRUPT"
        status_code = 422 if code in {"UPLOAD_INVALID_MEDIA", "UPLOAD_DIGEST_MISMATCH"} else 500
        raise _error(status_code, code)
    if upload.state != "receiving":
        raise _error(409, "UPLOAD_INVALID_STATE")


def _mark_failed(
    factory: sessionmaker[Session], upload_id: uuid.UUID, code: str
) -> None:
    with db_transaction(factory) as session:
        upload = session.scalar(
            select(UploadHandoff).where(UploadHandoff.id == upload_id).with_for_update()
        )
        if upload is None or upload.state == "complete":
            return
        upload.state = "failed"
        upload.error_code = code
        upload.expires_at = _now()
        intent = _intent_for_upload(session, upload.owner_id, upload_id, "cleanup_pending")
        intent.error_code = None


def _storage_corrupt(
    factory: sessionmaker[Session], upload_id: uuid.UUID, exc: StorageFailure
) -> HTTPException:
    if exc.errno == errno.EIO:
        try:
            _mark_failed(factory, upload_id, "UPLOAD_STORAGE_CORRUPT")
        except SQLAlchemyError:
            logger.warning("resumable_upload_failure_record_pending upload_id=%s", upload_id.hex)
        return _error(500, "UPLOAD_STORAGE_CORRUPT")
    return _error(507, "UPLOAD_STORAGE_FAILED")


def _reconcile(
    factory: sessionmaker[Session], store: MediaStore, upload: UploadHandoff
) -> list[UploadChunk]:
    chunks = _load_chunks(factory, upload.id)
    try:
        store.reconcile_resumable_part(
            upload.id, upload.confirmed_offset, _chunk_manifest(chunks)
        )
    except StorageFailure as exc:
        raise _storage_corrupt(factory, upload.id, exc) from exc
    return chunks


def get_upload_status(
    factory: sessionmaker[Session], store: MediaStore, owner_id: uuid.UUID, upload_id: uuid.UUID
) -> UploadStatusData:
    _load_owned_upload(factory, upload_id, owner_id)
    lease = _acquire_lease(store, upload_id)
    try:
        upload = _load_owned_upload(factory, upload_id, owner_id)
        if upload.state in {"receiving", "validating"}:
            if upload.expires_at is not None and upload.expires_at <= _now():
                raise _error(410, "UPLOAD_EXPIRED")
            _reconcile(factory, store, upload)
        chunks = _load_chunks(factory, upload.id)
        video_id: uuid.UUID | None = None
        if upload.state == "complete":
            with factory() as session:
                video_id = session.scalar(
                    select(Video.id).where(
                        Video.upload_id == upload.id,
                        Video.owner_id == owner_id,
                    )
                )
        total = upload.declared_total_bytes or upload.byte_count
        return {
            "upload_id": upload.id,
            "state": upload.state,
            "chunk_size": CHUNK_SIZE,
            "total_bytes": total,
            "confirmed_offset": upload.confirmed_offset,
            "next_chunk_index": len(chunks),
            "expires_at": upload.expires_at or upload.updated_at,
            "chunks": [
                {
                    "chunk_index": chunk.chunk_index,
                    "byte_offset": chunk.byte_offset,
                    "byte_count": chunk.byte_count,
                    "sha256": chunk.content_sha256,
                }
                for chunk in chunks
            ],
            "error_code": upload.error_code,
            "video_id": video_id,
        }
    finally:
        lease.release()


async def write_upload_chunk(
    request: Request,
    factory: sessionmaker[Session],
    store: MediaStore,
    owner_id: uuid.UUID,
    upload_id: uuid.UUID,
    chunk_index: int,
    declared_sha256: str,
) -> UploadChunkResponseData:
    if chunk_index < 0 or not _SHA256_RE.fullmatch(declared_sha256):
        raise _error(422, "INVALID_UPLOAD_METADATA")
    _load_owned_upload(factory, upload_id, owner_id)
    lease = _acquire_lease(store, upload_id)
    part_file = None
    try:
        upload = _load_owned_upload(factory, upload_id, owner_id)
        _check_mutable_state(upload, _now())
        total_bytes = upload.declared_total_bytes
        expected_sha = upload.expected_content_sha256
        if total_bytes is None or expected_sha is None:
            raise _error(500, "UPLOAD_STORAGE_CORRUPT")
        # 先按数据库记录的确认偏移校准磁盘文件，再判断分片是重放还是下一片。
        chunks = _reconcile(factory, store, upload)
        next_index = len(chunks)
        replay = chunk_index < next_index
        existing: UploadChunk | None = None
        if replay:
            existing = next((chunk for chunk in chunks if chunk.chunk_index == chunk_index), None)
            if existing is None:
                raise _error(500, "UPLOAD_STORAGE_CORRUPT")
            if existing.content_sha256 != declared_sha256:
                raise _error(409, "UPLOAD_CHUNK_CONFLICT", confirmed_offset=upload.confirmed_offset)
            expected_length = existing.byte_count
            expected_offset = existing.byte_offset
        elif chunk_index > next_index:
            raise _error(
                409,
                "UPLOAD_OUT_OF_ORDER",
                confirmed_offset=upload.confirmed_offset,
                next_chunk_index=next_index,
            )
        else:
            if upload.confirmed_offset >= total_bytes:
                raise _error(409, "UPLOAD_INVALID_STATE")
            expected_offset = upload.confirmed_offset
            expected_length = min(CHUNK_SIZE, total_bytes - expected_offset)
            part_file = store.open_part_at_offset(upload_id, expected_offset)

        actual_digest = hashlib.sha256()
        actual_count = 0
        try:
            async for body_chunk in request.stream():
                if not body_chunk:
                    continue
                if actual_count + len(body_chunk) > expected_length:
                    if part_file is not None:
                        allowed = max(0, expected_length - actual_count)
                        if allowed:
                            store.write_chunk(part_file, body_chunk[:allowed])
                    code = "UPLOAD_CHUNK_CONFLICT" if replay else "CHUNK_LENGTH_MISMATCH"
                    raise _error(409 if replay else 422, code)
                actual_count += len(body_chunk)
                actual_digest.update(body_chunk)
                if part_file is not None:
                    store.write_chunk(part_file, body_chunk)
        except (ClientDisconnect, asyncio.CancelledError):
            raise

        if actual_count != expected_length:
            code = "UPLOAD_CHUNK_CONFLICT" if replay else "CHUNK_LENGTH_MISMATCH"
            raise _error(409 if replay else 422, code)
        actual_sha = actual_digest.hexdigest()
        if replay:
            if existing is None or actual_sha != existing.content_sha256:
                raise _error(409, "UPLOAD_CHUNK_CONFLICT", confirmed_offset=upload.confirmed_offset)
        elif actual_sha != declared_sha256:
            raise _error(422, "CHUNK_DIGEST_MISMATCH")

        if not replay:
            try:
                assert part_file is not None
                store.flush_part(part_file)
                part_file.close()
                part_file = None
            except OSError as exc:
                raise _error(507, "UPLOAD_STORAGE_FAILED") from exc

            expires_at = _now() + UPLOAD_TTL
            try:
                with db_transaction(factory) as session:
                    current = session.scalar(
                        select(UploadHandoff)
                        .where(
                            UploadHandoff.id == upload_id,
                            UploadHandoff.owner_id == owner_id,
                            UploadHandoff.protocol_type == "resumable",
                        )
                        .with_for_update()
                    )
                    if current is None or current.state != "receiving":
                        raise _error(409, "UPLOAD_INVALID_STATE")
                    if current.confirmed_offset != expected_offset:
                        raise _error(409, "UPLOAD_OUT_OF_ORDER", confirmed_offset=current.confirmed_offset)
                    session.add(
                        UploadChunk(
                            upload_id=upload_id,
                            chunk_index=chunk_index,
                            byte_offset=expected_offset,
                            byte_count=expected_length,
                            content_sha256=actual_sha,
                        )
                    )
                    current.confirmed_offset = expected_offset + expected_length
                    current.byte_count = current.confirmed_offset
                    current.expires_at = expires_at
            except SQLAlchemyError as exc:
                logger.warning("resumable_chunk_confirmation_pending upload_id=%s", upload_id.hex)
                raise _error(503, "UPLOAD_CONFIRMATION_PENDING") from exc
            try:
                store.remember_resumable_prefix(upload_id, expected_offset + expected_length)
            except StorageFailure as exc:
                logger.warning("resumable_chunk_confirmation_pending upload_id=%s", upload_id.hex)
                raise _error(503, "UPLOAD_CONFIRMATION_PENDING") from exc
            confirmed_offset = expected_offset + expected_length
        else:
            expires_at = _now() + UPLOAD_TTL
            with db_transaction(factory) as session:
                current = session.scalar(
                    select(UploadHandoff)
                    .where(
                        UploadHandoff.id == upload_id,
                        UploadHandoff.owner_id == owner_id,
                        UploadHandoff.protocol_type == "resumable",
                    )
                    .with_for_update()
                )
                if current is None or current.state != "receiving":
                    raise _error(409, "UPLOAD_INVALID_STATE")
                current.expires_at = expires_at
                confirmed_offset = current.confirmed_offset

        return {
            "upload_id": upload_id,
            "state": "receiving",
            "chunk_index": chunk_index,
            "byte_offset": expected_offset,
            "byte_count": expected_length,
            "sha256": actual_sha,
            "confirmed_offset": confirmed_offset,
            "expires_at": expires_at,
            "replayed": replay,
        }
    except StorageFailure as exc:
        raise _error(507, "UPLOAD_STORAGE_FAILED") from exc
    except OSError as exc:
        raise _error(507, "UPLOAD_STORAGE_FAILED") from exc
    finally:
        if part_file is not None:
            part_file.close()
        lease.release()


def _find_video(factory: sessionmaker[Session], upload_id: uuid.UUID) -> Video | None:
    with factory() as session:
        return session.scalar(select(Video).where(Video.upload_id == upload_id))


def _find_upload(factory: sessionmaker[Session], upload_id: uuid.UUID) -> UploadHandoff | None:
    with factory() as session:
        return session.get(UploadHandoff, upload_id)


def _promoted_content_matches(store: MediaStore, upload: UploadHandoff, *, target: bool) -> bool:
    try:
        byte_count, digest_value = (
            store.media_sha256(upload.target_name) if target else store.file_sha256(upload.id)
        )
    except StorageFailure:
        return False
    return byte_count == upload.byte_count and digest_value == upload.content_sha256


def _finish_promoting_upload(
    factory: sessionmaker[Session], store: MediaStore, upload_id: uuid.UUID
) -> Video | None:
    upload = _find_upload(factory, upload_id)
    if upload is None or upload.state != "promoting":
        return _find_video(factory, upload_id)
    existing = _find_video(factory, upload_id)
    if existing is not None:
        _complete_upload(factory, upload_id)
        return existing
    if store.media_exists(upload.target_name):
        if not _promoted_content_matches(store, upload, target=True):
            raise StorageFailure(errno.EIO, "promoted upload content is corrupt")
    elif store.part_exists(upload.id):
        if not _promoted_content_matches(store, upload, target=False):
            raise StorageFailure(errno.EIO, "promoted upload content is corrupt")
        store.promote(upload.id, upload.target_name)
        if not _promoted_content_matches(store, upload, target=True):
            raise StorageFailure(errno.EIO, "promoted upload content is corrupt")
    else:
        raise StorageFailure(errno.EIO, "promoted upload file is missing")
    _complete_upload(factory, upload_id)
    return _find_video(factory, upload_id)


async def complete_upload_session(
    factory: sessionmaker[Session],
    settings: Settings,
    store: MediaStore,
    owner_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> Video:
    _load_owned_upload(factory, upload_id, owner_id)
    lease = _acquire_lease(store, upload_id)
    try:
        upload = _load_owned_upload(factory, upload_id, owner_id)
        if upload.state == "complete":
            video = _find_video(factory, upload_id)
            if video is None:
                raise _error(500, "UPLOAD_STORAGE_CORRUPT")
            return video
        if upload.state == "promoting":
            try:
                video = _finish_promoting_upload(factory, store, upload_id)
            except SQLAlchemyError as exc:
                raise _error(503, "UPLOAD_FINALIZATION_PENDING") from exc
            except StorageFailure as exc:
                try:
                    _mark_failed(factory, upload_id, "UPLOAD_STORAGE_CORRUPT")
                except SQLAlchemyError:
                    logger.warning("resumable_upload_failure_record_pending upload_id=%s", upload_id.hex)
                raise _error(500, "UPLOAD_STORAGE_CORRUPT") from exc
            if video is None:
                raise _error(503, "UPLOAD_FINALIZATION_PENDING")
            return video
        if upload.state == "failed":
            _check_mutable_state(upload, _now())
        if upload.state not in {"receiving", "validating"}:
            _check_mutable_state(upload, _now())
            raise _error(409, "UPLOAD_INVALID_STATE")
        now = _now()
        if upload.expires_at is not None and upload.expires_at <= now:
            raise _error(410, "UPLOAD_EXPIRED")
        total_bytes = upload.declared_total_bytes
        expected_sha = upload.expected_content_sha256
        if total_bytes is None or expected_sha is None:
            raise _error(500, "UPLOAD_STORAGE_CORRUPT")
        _reconcile(factory, store, upload)
        if upload.confirmed_offset != total_bytes:
            raise _error(
                409,
                "UPLOAD_INCOMPLETE",
                confirmed_offset=upload.confirmed_offset,
                total_bytes=total_bytes,
            )
        try:
            byte_count, actual_sha = store.file_sha256(upload_id)
        except StorageFailure as exc:
            raise _storage_corrupt(factory, upload_id, exc) from exc
        if byte_count != total_bytes:
            try:
                _mark_failed(factory, upload_id, "UPLOAD_STORAGE_CORRUPT")
            except SQLAlchemyError:
                logger.warning("resumable_upload_failure_record_pending upload_id=%s", upload_id.hex)
            raise _error(500, "UPLOAD_STORAGE_CORRUPT")
        if actual_sha != expected_sha:
            _mark_failed(factory, upload_id, "UPLOAD_DIGEST_MISMATCH")
            raise _error(422, "UPLOAD_DIGEST_MISMATCH")

        with db_transaction(factory) as session:
            current = session.scalar(
                select(UploadHandoff)
                .where(
                    UploadHandoff.id == upload_id,
                    UploadHandoff.owner_id == owner_id,
                )
                .with_for_update()
            )
            if current is None:
                raise _error(404, "UPLOAD_NOT_FOUND")
            if current.state not in {"receiving", "validating"}:
                raise _error(409, "UPLOAD_INVALID_STATE")
            current.state = "validating"
            current.byte_count = total_bytes
            current.content_sha256 = actual_sha
            current.expires_at = _now() + UPLOAD_TTL

        try:
            probe_task = asyncio.create_task(
                asyncio.to_thread(
                    probe_media,
                    store.part_path(upload_id),
                    settings.ffprobe_path,
                    float(settings.ffprobe_timeout_seconds),
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
            if exc.code in {
                "MEDIA_TIMEOUT",
                "MEDIA_STOP_UNCONFIRMED",
                "FFPROBE_UNAVAILABLE",
                "MEDIA_PROCESS_FAILED",
            }:
                raise _error(503, "UPLOAD_MEDIA_VALIDATION_FAILED") from exc
            _mark_failed(factory, upload_id, "UPLOAD_INVALID_MEDIA")
            raise _error(422, "UPLOAD_INVALID_MEDIA") from exc

        _set_upload_promoting(factory, upload_id, media)
        try:
            store.promote(upload_id, upload_id.hex)
            _complete_upload(factory, upload_id)
        except (SQLAlchemyError, OSError, RuntimeError) as exc:
            recovered = _find_video(factory, upload_id)
            if recovered is not None:
                return recovered
            if isinstance(exc, OSError):
                raise _error(507, "UPLOAD_STORAGE_FAILED") from exc
            raise _error(503, "UPLOAD_FINALIZATION_PENDING") from exc
        video = _find_video(factory, upload_id)
        if video is None:
            raise _error(503, "UPLOAD_FINALIZATION_PENDING")
        return video
    finally:
        lease.release()


def _set_cleanup_intent_failure(
    factory: sessionmaker[Session], upload_id: uuid.UUID, code: str
) -> None:
    with db_transaction(factory) as session:
        intent = session.scalar(
            select(CleanupIntent)
            .where(CleanupIntent.upload_id == upload_id, CleanupIntent.kind == "upload")
            .with_for_update()
        )
        if intent is not None:
            intent.state = "cleanup_failed"
            intent.error_code = code


def _cleanup_upload_locked(
    factory: sessionmaker[Session], store: MediaStore, upload_id: uuid.UUID, *,
    owner_id: uuid.UUID | None, abandoned: bool, now: datetime,
) -> tuple[str, bool]:
    with db_transaction(factory) as session:
        statement = select(UploadHandoff).where(UploadHandoff.id == upload_id).with_for_update()
        upload = session.scalar(statement)
        if upload is None:
            return "absent", True
        if owner_id is not None and upload.owner_id != owner_id:
            raise _error(404, "UPLOAD_NOT_FOUND")
        if upload.protocol_type != "resumable":
            raise _error(409, "UPLOAD_INVALID_STATE")
        if upload.state == "complete":
            return "complete", False
        registered_video = session.scalar(select(Video.id).where(Video.upload_id == upload_id))
        if registered_video is not None:
            upload.state = "complete"
            session.query(CleanupIntent).filter(CleanupIntent.upload_id == upload_id).delete(
                synchronize_session=False
            )
            return "complete", False
        if upload.state == "promoting":
            # A persisted promotion is a commit intent. Recover it before allowing
            # abandon or expiry cleanup to remove a valid target file.
            pass
        elif abandoned:
            upload.state = "abandoned"
        elif upload.state in {"receiving", "validating"}:
            if upload.expires_at is None or upload.expires_at > now:
                return upload.state, False
            upload.state = "expired"
        elif upload.state not in {"expired", "abandoned", "failed", "promoting"}:
            return upload.state, False
        intent = _intent_for_upload(session, upload.owner_id, upload_id, "cleanup_pending")
        intent.error_code = None
        state = upload.state

    if state == "promoting":
        try:
            video = _finish_promoting_upload(factory, store, upload_id)
        except SQLAlchemyError:
            return "promoting", False
        except StorageFailure:
            try:
                _mark_failed(factory, upload_id, "UPLOAD_STORAGE_CORRUPT")
            except SQLAlchemyError:
                return "promoting", False
        else:
            if video is not None:
                return "complete", False
            return "promoting", False

    try:
        store.remove_part(upload_id)
        store.remove_media(upload_id.hex)
    except OSError:
        try:
            _set_cleanup_intent_failure(factory, upload_id, "UPLOAD_STORAGE_FAILED")
        except SQLAlchemyError:
            logger.warning("resumable_cleanup_intent_pending upload_id=%s", upload_id.hex)
        return state, False

    with db_transaction(factory) as session:
        upload = session.scalar(
            select(UploadHandoff).where(UploadHandoff.id == upload_id).with_for_update()
        )
        if upload is None:
            return "absent", True
        if session.scalar(select(Video.id).where(Video.upload_id == upload_id)) is not None:
            upload.state = "complete"
            return "complete", False
        if upload.state == "complete":
            return "complete", False
        session.delete(upload)
    return "cleaned", True


def abandon_upload_session(
    factory: sessionmaker[Session], store: MediaStore, owner_id: uuid.UUID, upload_id: uuid.UUID
) -> tuple[int, str | None]:
    with factory() as session:
        initial = session.get(UploadHandoff, upload_id)
        if initial is None:
            return 204, None
        if initial.owner_id != owner_id:
            raise _error(404, "UPLOAD_NOT_FOUND")
        if initial.protocol_type != "resumable":
            raise _error(409, "UPLOAD_INVALID_STATE")
        if initial.state == "complete":
            raise _error(409, "UPLOAD_ALREADY_COMPLETED")
    lease = _acquire_lease(store, upload_id)
    try:
        try:
            state, cleaned = _cleanup_upload_locked(
                factory, store, upload_id, owner_id=owner_id, abandoned=True, now=_now()
            )
        except HTTPException:
            raise
        except SQLAlchemyError as exc:
            raise _error(503, "UPLOAD_CLEANUP_PENDING") from exc
        if state == "complete":
            raise _error(409, "UPLOAD_ALREADY_COMPLETED")
        if cleaned:
            return 204, None
        return 202, "UPLOAD_CLEANUP_PENDING"
    finally:
        lease.release()


def reclaim_expired_uploads(
    factory: sessionmaker[Session],
    store: MediaStore,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    current_time = now or _now()
    if limit <= 0:
        return 0
    with factory() as session:
        candidate_ids = list(
            session.scalars(
                select(UploadHandoff.id)
                .where(
                    UploadHandoff.protocol_type == "resumable",
                    UploadHandoff.state != "complete",
                    (
                        UploadHandoff.state.in_(["failed", "expired", "abandoned"])
                        | (
                            UploadHandoff.state.in_(["receiving", "validating", "promoting"])
                            & (UploadHandoff.expires_at <= current_time)
                        )
                    ),
                )
                .order_by(UploadHandoff.expires_at, UploadHandoff.created_at)
                .limit(limit)
            )
        )
    removed = 0
    for upload_id in candidate_ids:
        try:
            lease = store.acquire_lease(upload_id)
        except OSError:
            logger.warning("resumable_recovery_cleanup_pending upload_id=%s", upload_id.hex)
            continue
        if lease is None:
            continue
        try:
            try:
                _, cleaned = _cleanup_upload_locked(
                    factory,
                    store,
                    upload_id,
                    owner_id=None,
                    abandoned=False,
                    now=current_time,
                )
            except (HTTPException, SQLAlchemyError, OSError):
                logger.warning("resumable_recovery_cleanup_pending upload_id=%s", upload_id.hex)
                continue
            removed += int(cleaned)
        finally:
            lease.release()
    return removed


def recover_resumable_uploads_on_startup(
    factory: sessionmaker[Session], store: MediaStore
) -> None:
    with factory() as session:
        upload_ids = list(
            session.scalars(
                select(UploadHandoff.id)
                .where(
                    UploadHandoff.protocol_type == "resumable",
                    UploadHandoff.state != "complete",
                )
                .order_by(UploadHandoff.created_at)
            )
        )
    now = _now()
    for upload_id in upload_ids:
        lease = store.acquire_lease(upload_id)
        if lease is None:
            continue
        try:
            upload = _find_upload(factory, upload_id)
            if upload is None or upload.state == "complete":
                continue
            if upload.state in {"receiving", "validating"} and (
                upload.expires_at is None or upload.expires_at > now
            ):
                continue
            try:
                _cleanup_upload_locked(
                    factory,
                    store,
                    upload_id,
                    owner_id=None,
                    abandoned=False,
                    now=now,
                )
            except (HTTPException, SQLAlchemyError, OSError):
                logger.warning("resumable_recovery_cleanup_pending upload_id=%s", upload_id.hex)
        finally:
            lease.release()
