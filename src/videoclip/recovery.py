from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from videoclip.models import CleanupIntent, UploadHandoff, Video
from videoclip.resumable_uploads import recover_resumable_uploads_on_startup
from videoclip.services import (
    _intent_for_upload,
    db_transaction,
    delete_video,
    recover_confirmed_attempt_cleanup,
)
from videoclip.settings import Settings
from videoclip.storage import MediaStore

logger = logging.getLogger("videoclip.recovery")


def _digest_file(path: Path, timeout_seconds: float) -> str:
    import time

    deadline = time.monotonic() + timeout_seconds
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            if time.monotonic() > deadline:
                return ""
            digest.update(chunk)
    return digest.hexdigest()


def _is_promoted_upload_valid(store: MediaStore, upload: UploadHandoff, timeout_seconds: float) -> bool:
    if upload.part_name != f"{upload.id.hex}.part" or upload.target_name != upload.id.hex:
        return False
    part = store.part_path(upload.id)
    target = store.target_path(upload.target_name)

    def matches(path: Path) -> bool:
        return (
            path.is_file()
            and path.stat().st_size == upload.byte_count
            and upload.content_sha256 is not None
            and _digest_file(path, timeout_seconds) == upload.content_sha256
        )

    if matches(target):
        if part.exists():
            store.remove_part(upload.id)
        return True
    if not matches(part):
        return False
    store.promote(upload.id, upload.target_name)
    return matches(target)


def _complete_promoted(factory: sessionmaker[Session], upload_id: uuid.UUID) -> bool:
    with db_transaction(factory) as session:
        upload = session.scalar(select(UploadHandoff).where(UploadHandoff.id == upload_id).with_for_update())
        if upload is None:
            return False
        if upload.state == "complete":
            return True
        if (
            upload.state != "promoting"
            or upload.content_sha256 is None
            or upload.duration_seconds is None
            or upload.media_info is None
        ):
            return False
        existing = session.scalar(select(Video).where(Video.upload_id == upload_id))
        if existing is None:
            session.add(
                Video(
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
            )
        upload.state = "complete"
        session.query(CleanupIntent).filter(CleanupIntent.upload_id == upload_id).delete(
            synchronize_session=False
        )
    return True


def _mark_cleanup_pending(factory: sessionmaker[Session], upload_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    with db_transaction(factory) as session:
        upload = session.scalar(select(UploadHandoff).where(UploadHandoff.id == upload_id).with_for_update())
        if upload is None or upload.state == "complete":
            return
        upload.state = "cleanup_pending"
        upload.error_code = "RECOVERY_REQUIRED"
        _intent_for_upload(session, owner_id, upload_id, "cleanup_pending")


def _discard_interrupted(factory: sessionmaker[Session], store: MediaStore, upload_id: uuid.UUID) -> bool:
    with factory() as session:
        upload = session.get(UploadHandoff, upload_id)
        if upload is None:
            return False
        owner_id = upload.owner_id
    _mark_cleanup_pending(factory, upload_id, owner_id)
    try:
        store.remove_part(upload_id)
        store.remove_media(upload_id.hex)
    except OSError:
        logger.warning("upload_recovery_cleanup_pending upload_id=%s", upload_id.hex)
        return False
    with db_transaction(factory) as session:
        session.query(CleanupIntent).filter(CleanupIntent.upload_id == upload_id).delete(
            synchronize_session=False
        )
        session.query(UploadHandoff).filter(UploadHandoff.id == upload_id).delete(synchronize_session=False)
    return True


def _recover_one(
    factory: sessionmaker[Session], settings: Settings, store: MediaStore, upload_id: uuid.UUID
) -> None:
    owner_id: uuid.UUID | None = None
    lease = store.acquire_lease(upload_id)
    if lease is None:
        return
    remove_lease_after_release = False
    try:
        with db_transaction(factory) as session:
            upload = session.scalar(
                select(UploadHandoff).where(UploadHandoff.id == upload_id).with_for_update()
            )
            if upload is None or upload.state == "complete":
                return
            state = upload.state
            owner_id = upload.owner_id
        if state != "promoting":
            remove_lease_after_release = _discard_interrupted(factory, store, upload_id)
            return
        with factory() as session:
            upload = session.get(UploadHandoff, upload_id)
            if upload is None:
                return
            valid = _is_promoted_upload_valid(store, upload, settings.upload_total_timeout_seconds)
        if valid:
            if _complete_promoted(factory, upload_id):
                return
        remove_lease_after_release = _discard_interrupted(factory, store, upload_id)
    except OSError:
        logger.warning("upload_recovery_pending upload_id=%s", upload_id.hex)
        try:
            if owner_id is not None:
                _mark_cleanup_pending(factory, upload_id, owner_id)
        except OSError:
            pass
    finally:
        lease.release()
        if remove_lease_after_release:
            try:
                store.remove_lease(upload_id)
            except OSError:
                logger.warning("upload_lease_cleanup_pending upload_id=%s", upload_id.hex)


def recover_interrupted_work(factory: sessionmaker[Session], settings: Settings, store: MediaStore) -> None:
    store.prepare()
    recover_resumable_uploads_on_startup(factory, store)
    with factory() as session:
        pending_upload_ids = list(
            session.scalars(
                select(UploadHandoff.id)
                .where(
                    UploadHandoff.protocol_type == "ordinary",
                    UploadHandoff.state != "complete",
                )
                .order_by(UploadHandoff.created_at)
            )
        )
        pending_deletions = list(
            session.execute(
                select(Video.owner_id, Video.id).where(Video.status.in_(("deleting", "cleanup_failed")))
            ).all()
        )
    for upload_id in pending_upload_ids:
        _recover_one(factory, settings, store, upload_id)
    for owner_id, video_id in pending_deletions:
        try:
            delete_video(factory, store, owner_id, video_id)
        except Exception:
            logger.warning("video_recovery_cleanup_pending video_id=%s", video_id.hex)
    recover_confirmed_attempt_cleanup(factory, store)
