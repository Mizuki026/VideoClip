from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import ClientDisconnect, Request

from tests.helpers import csrf, login, mutate_headers, register
from videoclip import resumable_uploads
from videoclip.main import create_app
from videoclip.media import MediaInfo
from videoclip.models import CleanupIntent, UploadChunk, UploadHandoff, Video
from videoclip.recovery import recover_interrupted_work
from videoclip.settings import Settings
from videoclip.storage import MediaStore, StorageFailure


def _new_account(client: TestClient, username: str) -> None:
    assert register(client, username).status_code == 201
    assert login(client, username).status_code == 200


def _headers(client: TestClient, extra: dict[str, str] | None = None) -> dict[str, str]:
    result = mutate_headers(csrf(client))
    result.update(extra or {})
    return result


def _create_upload(client: TestClient, body: bytes, *, key: str | None = None):
    key = key or uuid4().hex
    response = client.post(
        "/uploads",
        json={
            "file_name": "course/video.mp4",
            "total_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        },
        headers=_headers(client, {"Idempotency-Key": key}),
    )
    return response, key


def _put_chunk(client: TestClient, upload_id: str, index: int, body: bytes):
    return client.put(
        f"/uploads/{upload_id}/chunks/{index}",
        content=body,
        headers=_headers(
            client,
            {
                "Content-Type": "application/octet-stream",
                "X-Chunk-SHA256": hashlib.sha256(body).hexdigest(),
            },
        ),
    )


def _fake_media(*_: object) -> MediaInfo:
    return MediaInfo(
        duration_seconds=1.4,
        container="mp4",
        video_codec="h264",
        pixel_format="yuv420p",
        audio_codec="aac",
        timeline_start_seconds=0.0,
        streams=[],
    )


def _create_app(test_settings: Settings, session_factory: sessionmaker[Session], store: MediaStore):
    return create_app(test_settings, session_factory, store)


def test_resumable_upload_protocol_is_sequential_idempotent_and_owner_scoped(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(tmp_path / "resumable-media")
    app = _create_app(test_settings, session_factory, store)
    body = b"a" * resumable_uploads.CHUNK_SIZE + b"tail"
    with TestClient(app) as owner, TestClient(app) as other:
        _new_account(owner, "resume-owner")
        _new_account(other, "resume-other")
        payload = {
            "file_name": "course/video.mp4",
            "total_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        key = uuid4().hex
        no_csrf = owner.post(
            "/uploads", json=payload, headers={"Origin": "http://127.0.0.1:58001", "Idempotency-Key": key}
        )
        assert no_csrf.status_code == 403

        created = owner.post("/uploads", json=payload, headers=_headers(owner, {"Idempotency-Key": key}))
        assert created.status_code == 201, created.text
        session = created.json()
        upload_id = session["upload_id"]
        assert session["chunk_size"] == resumable_uploads.CHUNK_SIZE
        assert session["confirmed_offset"] == 0
        repeated_create = owner.post(
            "/uploads", json=payload, headers=_headers(owner, {"Idempotency-Key": key})
        )
        assert repeated_create.status_code == 200
        assert repeated_create.json()["upload_id"] == upload_id
        conflict = owner.post(
            "/uploads",
            json={**payload, "total_bytes": len(body) + 1},
            headers=_headers(owner, {"Idempotency-Key": key}),
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "UPLOAD_IDEMPOTENCY_CONFLICT"
        assert other.get(f"/uploads/{upload_id}").status_code == 404

        out_of_order = _put_chunk(owner, upload_id, 1, b"tail")
        assert out_of_order.status_code == 409
        assert out_of_order.json()["detail"]["code"] == "UPLOAD_OUT_OF_ORDER"
        assert owner.get(f"/uploads/{upload_id}").json()["confirmed_offset"] == 0

        missing_csrf_chunk = owner.put(
            f"/uploads/{upload_id}/chunks/0",
            content=body[: resumable_uploads.CHUNK_SIZE],
            headers={
                "Origin": "http://127.0.0.1:58001",
                "Content-Type": "application/octet-stream",
                "X-Chunk-SHA256": hashlib.sha256(body[: resumable_uploads.CHUNK_SIZE]).hexdigest(),
            },
        )
        assert missing_csrf_chunk.status_code == 403
        missing_csrf_delete = owner.delete(
            f"/uploads/{upload_id}", headers={"Origin": "http://127.0.0.1:58001"}
        )
        assert missing_csrf_delete.status_code == 403

        first = body[: resumable_uploads.CHUNK_SIZE]
        accepted = _put_chunk(owner, upload_id, 0, first)
        assert accepted.status_code == 200 and accepted.json()["replayed"] is False
        assert accepted.json()["confirmed_offset"] == resumable_uploads.CHUNK_SIZE
        replay = _put_chunk(owner, upload_id, 0, first)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["confirmed_offset"] == resumable_uploads.CHUNK_SIZE
        different = b"z" * resumable_uploads.CHUNK_SIZE
        conflict_chunk = owner.put(
            f"/uploads/{upload_id}/chunks/0",
            content=different,
            headers=_headers(
                owner,
                {
                    "Content-Type": "application/octet-stream",
                    "X-Chunk-SHA256": hashlib.sha256(different).hexdigest(),
                },
            ),
        )
        assert conflict_chunk.status_code == 409
        assert conflict_chunk.json()["detail"]["code"] == "UPLOAD_CHUNK_CONFLICT"
        assert owner.get(f"/uploads/{upload_id}").json()["confirmed_offset"] == resumable_uploads.CHUNK_SIZE

        early = owner.post(f"/uploads/{upload_id}/complete", headers=_headers(owner))
        assert early.status_code == 409
        assert early.json()["detail"]["code"] == "UPLOAD_INCOMPLETE"
        last = _put_chunk(owner, upload_id, 1, b"tail")
        assert last.status_code == 200
        assert last.json()["confirmed_offset"] == len(body)
        status = owner.get(f"/uploads/{upload_id}")
        assert status.status_code == 200
        assert [chunk["byte_count"] for chunk in status.json()["chunks"]] == [
            resumable_uploads.CHUNK_SIZE,
            4,
        ]

        without_csrf = owner.post(f"/uploads/{upload_id}/complete")
        assert without_csrf.status_code == 403
        monkeypatch.setattr(resumable_uploads, "probe_media", _fake_media)
        completed = owner.post(f"/uploads/{upload_id}/complete", headers=_headers(owner))
        assert completed.status_code == 200, completed.text
        video_id = completed.json()["video"]["id"]
        assert completed.json()["video"]["byte_count"] == len(body)
        assert completed.json()["video"]["content_sha256"] == hashlib.sha256(body).hexdigest()
        repeated_complete = owner.post(f"/uploads/{upload_id}/complete", headers=_headers(owner))
        assert repeated_complete.status_code == 200
        assert repeated_complete.json()["video"]["id"] == video_id
        assert owner.get(f"/videos/{video_id}").status_code == 200
        assert owner.head(f"/videos/{video_id}/content").headers["content-length"] == str(len(body))
        assert owner.get(f"/videos/{video_id}/content", headers={"Range": "bytes=4-20"}).content == body[4:21]
        assert other.delete(f"/uploads/{upload_id}", headers=_headers(other)).status_code == 404

        delete_completed = owner.delete(f"/uploads/{upload_id}", headers=_headers(owner))
        assert delete_completed.status_code == 409
        assert delete_completed.json()["detail"]["code"] == "UPLOAD_ALREADY_COMPLETED"
        assert store.target_path(upload_id.replace("-", "")).exists()
        assert owner.delete(f"/videos/{video_id}", headers=_headers(owner)).status_code == 204


def test_invalid_chunk_length_and_digest_do_not_advance_confirmed_offset(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path
) -> None:
    store = MediaStore(tmp_path / "chunk-validation")
    app = _create_app(test_settings, session_factory, store)
    with TestClient(app) as client:
        _new_account(client, "chunk-validation")
        body = b"four"
        created, _ = _create_upload(client, body)
        upload_id = created.json()["upload_id"]

        short = client.put(
            f"/uploads/{upload_id}/chunks/0",
            content=b"four"[:3],
            headers=_headers(
                client,
                {
                    "Content-Type": "application/octet-stream",
                    "X-Chunk-SHA256": hashlib.sha256(b"four"[:3]).hexdigest(),
                },
            ),
        )
        assert short.status_code == 422
        assert short.json()["detail"]["code"] == "CHUNK_LENGTH_MISMATCH"

        wrong_digest = client.put(
            f"/uploads/{upload_id}/chunks/0",
            content=body,
            headers=_headers(
                client,
                {
                    "Content-Type": "application/octet-stream",
                    "X-Chunk-SHA256": hashlib.sha256(b"else").hexdigest(),
                },
            ),
        )
        assert wrong_digest.status_code == 422
        assert wrong_digest.json()["detail"]["code"] == "CHUNK_DIGEST_MISMATCH"
        assert client.get(f"/uploads/{upload_id}").json()["confirmed_offset"] == 0
        valid = _put_chunk(client, upload_id, 0, body)
        assert valid.status_code == 200
        assert valid.json()["confirmed_offset"] == len(body)


def test_disconnect_and_api_restart_preserve_confirmed_prefix_and_truncate_tail(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path
) -> None:
    store = MediaStore(tmp_path / "resume-after-restart")
    app = _create_app(test_settings, session_factory, store)
    with TestClient(app) as client:
        _new_account(client, "resume-restart")
        body = b"x" * resumable_uploads.CHUNK_SIZE + b"last"
        created, _ = _create_upload(client, body)
        upload_id = created.json()["upload_id"]
        owner_id = client.get("/auth/me").json()["id"]
        assert _put_chunk(client, upload_id, 0, body[: resumable_uploads.CHUNK_SIZE]).status_code == 200

    partial = b"cut"
    messages = iter(
        (
            {"type": "http.request", "body": partial, "more_body": True},
            {"type": "http.disconnect"},
        )
    )

    async def receive():
        return next(messages)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": f"/uploads/{upload_id}/chunks/1",
        "raw_path": f"/uploads/{upload_id}/chunks/1".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/octet-stream")],
        "client": ("127.0.0.1", 43210),
        "server": ("127.0.0.1", 58001),
    }
    request = Request(scope, receive)
    with pytest.raises(ClientDisconnect):
        asyncio.run(
            resumable_uploads.write_upload_chunk(
                request,
                session_factory,
                store,
                UUID(owner_id),
                UUID(upload_id),
                1,
                hashlib.sha256(body[-4:]).hexdigest(),
            )
        )
    part = store.part_path(UUID(upload_id))
    assert part.stat().st_size == resumable_uploads.CHUNK_SIZE + len(partial)

    recover_interrupted_work(session_factory, test_settings, store)
    with session_factory() as session:
        row = session.get(UploadHandoff, upload_id)
        assert row is not None and row.state == "receiving"
        assert row.confirmed_offset == resumable_uploads.CHUNK_SIZE
    assert part.stat().st_size == resumable_uploads.CHUNK_SIZE + len(partial)

    restarted = _create_app(test_settings, session_factory, store)
    with TestClient(restarted) as client:
        assert login(client, "resume-restart").status_code == 200
        status = client.get(f"/uploads/{upload_id}")
        assert status.status_code == 200
        assert status.json()["confirmed_offset"] == resumable_uploads.CHUNK_SIZE
        assert part.stat().st_size == resumable_uploads.CHUNK_SIZE
        last = _put_chunk(client, upload_id, 1, body[-4:])
        assert last.status_code == 200
        assert last.json()["confirmed_offset"] == len(body)


def test_database_failure_before_chunk_commit_truncates_unconfirmed_bytes(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(tmp_path / "chunk-commit-failure")
    app = _create_app(test_settings, session_factory, store)
    body = b"database commit recovery"
    with TestClient(app) as client:
        _new_account(client, "chunk-commit")
        created, _ = _create_upload(client, body)
        upload_id = created.json()["upload_id"]
        original_transaction = resumable_uploads.db_transaction

        @contextmanager
        def fail_before_commit(factory):
            with original_transaction(factory) as session:
                yield session
                raise SQLAlchemyError("injected transaction failure")

        monkeypatch.setattr(resumable_uploads, "db_transaction", fail_before_commit)
        failed = _put_chunk(client, upload_id, 0, body)
        assert failed.status_code == 503
        assert failed.json()["detail"]["code"] == "UPLOAD_CONFIRMATION_PENDING"
        monkeypatch.setattr(resumable_uploads, "db_transaction", original_transaction)

        with session_factory() as session:
            row = session.get(UploadHandoff, upload_id)
            assert row is not None and row.confirmed_offset == 0
        part = store.part_path(UUID(upload_id))
        assert part.stat().st_size == len(body)
        status = client.get(f"/uploads/{upload_id}")
        assert status.status_code == 200 and status.json()["confirmed_offset"] == 0
        assert part.stat().st_size == 0
        accepted = _put_chunk(client, upload_id, 0, body)
        assert accepted.status_code == 200


def test_database_commit_then_lost_confirmation_is_reconciled_idempotently(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(tmp_path / "chunk-response-lost")
    app = _create_app(test_settings, session_factory, store)
    body = b"committed before response was lost"
    with TestClient(app) as client:
        _new_account(client, "chunk-response-lost")
        created, _ = _create_upload(client, body)
        upload_id = created.json()["upload_id"]

        def fail_after_commit(*_: object) -> None:
            raise StorageFailure(5, "injected response-side failure")

        monkeypatch.setattr(store, "remember_resumable_prefix", fail_after_commit)
        response = _put_chunk(client, upload_id, 0, body)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "UPLOAD_CONFIRMATION_PENDING"
        monkeypatch.undo()

        status = client.get(f"/uploads/{upload_id}")
        assert status.status_code == 200
        assert status.json()["confirmed_offset"] == len(body)
        replay = _put_chunk(client, upload_id, 0, body)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["confirmed_offset"] == len(body)


def test_resumable_disk_write_failure_does_not_confirm_chunk(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(tmp_path / "disk-write-failure")
    app = _create_app(test_settings, session_factory, store)
    body = b"disk write failure"
    with TestClient(app) as client:
        _new_account(client, "disk-write-failure")
        created, _ = _create_upload(client, body)
        upload_id = created.json()["upload_id"]

        def fail_write(*_: object) -> None:
            raise OSError("injected disk-full condition")

        monkeypatch.setattr(store, "write_chunk", fail_write)
        failed = _put_chunk(client, upload_id, 0, body)
        assert failed.status_code == 507
        assert failed.json()["detail"]["code"] == "UPLOAD_STORAGE_FAILED"
        assert client.get(f"/uploads/{upload_id}").json()["confirmed_offset"] == 0
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(UploadChunk)) == 0
            assert session.scalar(select(func.count()).select_from(Video)) == 0

        monkeypatch.undo()
        accepted = _put_chunk(client, upload_id, 0, body)
        assert accepted.status_code == 200
        assert accepted.json()["confirmed_offset"] == len(body)


def test_full_digest_mismatch_prevents_media_registration(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(tmp_path / "whole-digest-mismatch")
    app = _create_app(test_settings, session_factory, store)
    body = b"right bytes, wrong declared whole-file digest"
    with TestClient(app) as client:
        _new_account(client, "whole-digest-mismatch")
        created = client.post(
            "/uploads",
            json={
                "file_name": "mismatch.mp4",
                "total_bytes": len(body),
                "sha256": hashlib.sha256(b"different file").hexdigest(),
            },
            headers=_headers(client, {"Idempotency-Key": uuid4().hex}),
        )
        assert created.status_code == 201
        upload_id = created.json()["upload_id"]
        assert _put_chunk(client, upload_id, 0, body).status_code == 200
        monkeypatch.setattr(resumable_uploads, "probe_media", _fake_media)
        mismatch = client.post(f"/uploads/{upload_id}/complete", headers=_headers(client))
        assert mismatch.status_code == 422
        assert mismatch.json()["detail"]["code"] == "UPLOAD_DIGEST_MISMATCH"
        with session_factory() as session:
            row = session.get(UploadHandoff, upload_id)
            assert row is not None and row.state == "failed"
            assert row.confirmed_offset == len(body)
            assert session.scalar(select(Video).where(Video.upload_id == upload_id)) is None


@pytest.mark.parametrize("damage", ["short", "digest"])
def test_confirmed_file_corruption_fails_closed(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    damage: str,
) -> None:
    store = MediaStore(tmp_path / f"corrupt-{damage}")
    app = _create_app(test_settings, session_factory, store)
    body = b"confirmed bytes"
    with TestClient(app) as client:
        _new_account(client, f"corrupt-{damage}")
        created, _ = _create_upload(client, body)
        upload_id = created.json()["upload_id"]
        assert _put_chunk(client, upload_id, 0, body).status_code == 200
        path = store.part_path(UUID(upload_id))
        path.write_bytes(body[:2] if damage == "short" else b"corrupted bytes")
        store._verified_upload_prefixes.pop(UUID(upload_id), None)

        status = client.get(f"/uploads/{upload_id}")
        assert status.status_code == 500
        assert status.json()["detail"]["code"] == "UPLOAD_STORAGE_CORRUPT"
        with session_factory() as session:
            row = session.get(UploadHandoff, upload_id)
            assert row is not None
            assert row.state == "failed"
            assert row.confirmed_offset == len(body)
            assert session.scalar(select(func.count()).select_from(Video)) == 0


def test_rename_before_registration_recovers_to_one_video(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import videoclip.resumable_uploads as upload_service

    store = MediaStore(tmp_path / "promotion-recovery")
    app = _create_app(test_settings, session_factory, store)
    body = b"probed media content"
    with TestClient(app) as client:
        _new_account(client, "promotion-recovery")
        created, _ = _create_upload(client, body)
        upload_id = created.json()["upload_id"]
        assert _put_chunk(client, upload_id, 0, body).status_code == 200
        monkeypatch.setattr(upload_service, "probe_media", _fake_media)

        def fail_registration(*_: object, **__: object):
            raise SQLAlchemyError("injected failure after atomic rename")

        monkeypatch.setattr(upload_service, "_complete_upload", fail_registration)
        pending = client.post(f"/uploads/{upload_id}/complete", headers=_headers(client))
        assert pending.status_code == 503
        assert pending.json()["detail"]["code"] == "UPLOAD_FINALIZATION_PENDING"
        assert store.target_path(upload_id.replace("-", "")).exists()
        with session_factory() as session:
            row = session.get(UploadHandoff, upload_id)
            assert row is not None and row.state == "promoting"
            assert session.scalar(select(Video).where(Video.upload_id == upload_id)) is None

        monkeypatch.undo()
        recover_interrupted_work(session_factory, test_settings, store)
        with session_factory() as session:
            videos = list(session.scalars(select(Video).where(Video.upload_id == upload_id)))
            row = session.get(UploadHandoff, upload_id)
            assert len(videos) == 1 and row is not None and row.state == "complete"
        repeated = client.post(f"/uploads/{upload_id}/complete", headers=_headers(client))
        assert repeated.status_code == 200
        assert repeated.json()["video"]["id"] == str(videos[0].id)


def test_media_rejection_is_terminal_and_expired_cleanup_is_retryable(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import videoclip.resumable_uploads as upload_service

    store = MediaStore(tmp_path / "expiry-cleanup")
    app = _create_app(test_settings, session_factory, store)
    invalid = b"not a media file"
    with TestClient(app) as client:
        _new_account(client, "expiry-cleanup")
        created, _ = _create_upload(client, invalid)
        invalid_id = created.json()["upload_id"]
        assert _put_chunk(client, invalid_id, 0, invalid).status_code == 200

        def reject_media(*_: object):
            from videoclip.media import MediaError

            raise MediaError("INVALID_MEDIA", "invalid fixture")

        monkeypatch.setattr(upload_service, "probe_media", reject_media)
        rejected = client.post(f"/uploads/{invalid_id}/complete", headers=_headers(client))
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "UPLOAD_INVALID_MEDIA"
        with session_factory() as session:
            row = session.get(UploadHandoff, invalid_id)
            assert row is not None and row.state == "failed"
            assert session.scalar(select(Video).where(Video.upload_id == invalid_id)) is None
        monkeypatch.undo()
        assert resumable_uploads.reclaim_expired_uploads(session_factory, store) == 1
        assert not store.part_path(UUID(invalid_id)).exists()

        expired_body = b"expired bytes"
        expired_create, _ = _create_upload(client, expired_body)
        expired_id = expired_create.json()["upload_id"]
        assert _put_chunk(client, expired_id, 0, expired_body).status_code == 200
        with session_factory.begin() as session:
            row = session.get(UploadHandoff, expired_id)
            assert row is not None
            row.expires_at = row.created_at.replace(year=row.created_at.year - 1)

        assert client.get(f"/uploads/{expired_id}").status_code == 410
        assert _put_chunk(client, expired_id, 0, expired_body).json()["detail"]["code"] == "UPLOAD_EXPIRED"
        expired_uuid = UUID(expired_id)
        lease = store.acquire_lease(expired_uuid)
        assert lease is not None
        assert resumable_uploads.reclaim_expired_uploads(session_factory, store) == 0
        assert store.part_path(expired_uuid).exists()
        lease.release()
        assert resumable_uploads.reclaim_expired_uploads(session_factory, store) == 1
        assert not store.part_path(expired_uuid).exists()
        with session_factory() as session:
            assert session.get(UploadHandoff, expired_id) is None
            assert session.scalar(select(func.count()).select_from(UploadChunk)) == 0


def test_abandon_cleanup_failure_keeps_intent_for_reaper_retry(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(tmp_path / "abandon-retry")
    app = _create_app(test_settings, session_factory, store)
    with TestClient(app) as client:
        _new_account(client, "abandon-retry")
        created, _ = _create_upload(client, b"abandon me")
        upload_id = created.json()["upload_id"]
        original_remove = store.remove_part

        def fail_remove(_: object) -> None:
            raise PermissionError("injected cleanup failure")

        monkeypatch.setattr(store, "remove_part", fail_remove)
        pending = client.delete(f"/uploads/{upload_id}", headers=_headers(client))
        assert pending.status_code == 202
        assert pending.json()["detail"]["code"] == "UPLOAD_CLEANUP_PENDING"
        with session_factory() as session:
            row = session.get(UploadHandoff, upload_id)
            intent = session.scalar(select(CleanupIntent).where(CleanupIntent.upload_id == upload_id))
            assert row is not None and row.state == "abandoned"
            assert intent is not None and intent.state == "cleanup_failed"

        monkeypatch.setattr(store, "remove_part", original_remove)
        assert resumable_uploads.reclaim_expired_uploads(session_factory, store) == 1
        assert client.delete(f"/uploads/{upload_id}", headers=_headers(client)).status_code == 204


def test_active_lease_serializes_chunk_completion_abandon_and_reaping(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path
) -> None:
    store = MediaStore(tmp_path / "lease-contention")
    app = _create_app(test_settings, session_factory, store)
    with TestClient(app) as client:
        _new_account(client, "lease-contention")
        body = b"lease-protected content"
        created, _ = _create_upload(client, body)
        upload_id = created.json()["upload_id"]
        upload_uuid = UUID(upload_id)
        with session_factory.begin() as session:
            row = session.get(UploadHandoff, upload_id)
            assert row is not None
            row.expires_at = row.created_at.replace(year=row.created_at.year - 1)
        lease = store.acquire_lease(upload_uuid)
        assert lease is not None
        try:
            busy_write = _put_chunk(client, upload_id, 0, body)
            assert busy_write.status_code == 409
            assert busy_write.json()["detail"]["code"] == "UPLOAD_BUSY"
            busy_complete = client.post(f"/uploads/{upload_id}/complete", headers=_headers(client))
            assert busy_complete.status_code == 409
            assert busy_complete.json()["detail"]["code"] == "UPLOAD_BUSY"
            busy_abandon = client.delete(f"/uploads/{upload_id}", headers=_headers(client))
            assert busy_abandon.status_code == 409
            assert busy_abandon.json()["detail"]["code"] == "UPLOAD_BUSY"
            assert resumable_uploads.reclaim_expired_uploads(session_factory, store) == 0
            with session_factory() as session:
                assert session.get(UploadHandoff, upload_id) is not None
        finally:
            lease.release()


def test_completion_and_abandon_race_cannot_delete_registered_video(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import videoclip.resumable_uploads as upload_service

    store = MediaStore(tmp_path / "complete-abandon-race")
    app = _create_app(test_settings, session_factory, store)
    body = b"completion wins the active lease"
    entered_probe = threading.Event()
    release_probe = threading.Event()

    def slow_probe(*_: object) -> MediaInfo:
        entered_probe.set()
        assert release_probe.wait(10)
        return _fake_media()

    monkeypatch.setattr(upload_service, "probe_media", slow_probe)
    with TestClient(app) as owner, TestClient(app) as second_client:
        _new_account(owner, "complete-abandon-race")
        assert login(second_client, "complete-abandon-race").status_code == 200
        created, _ = _create_upload(owner, body)
        upload_id = created.json()["upload_id"]
        assert _put_chunk(owner, upload_id, 0, body).status_code == 200
        completed: dict[str, object] = {}

        def complete_request() -> None:
            completed["response"] = owner.post(
                f"/uploads/{upload_id}/complete", headers=_headers(owner)
            )

        worker = threading.Thread(target=complete_request)
        worker.start()
        assert entered_probe.wait(5)
        competing_abandon = second_client.delete(
            f"/uploads/{upload_id}", headers=_headers(second_client)
        )
        assert competing_abandon.status_code == 409
        assert competing_abandon.json()["detail"]["code"] == "UPLOAD_BUSY"
        release_probe.set()
        worker.join(15)
        assert not worker.is_alive()
        final_response = completed["response"]
        assert final_response.status_code == 200
        video_id = final_response.json()["video"]["id"]
        after_complete = second_client.delete(f"/uploads/{upload_id}", headers=_headers(second_client))
        assert after_complete.status_code == 409
        assert after_complete.json()["detail"]["code"] == "UPLOAD_ALREADY_COMPLETED"
        assert second_client.head(f"/videos/{video_id}/content").status_code == 200
