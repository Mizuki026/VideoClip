from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.helpers import csrf, login, mutate_headers, register
from videoclip.main import create_app
from videoclip.models import CleanupIntent, UploadHandoff, User, Video
from videoclip.recovery import recover_interrupted_work
from videoclip.services import _create_handoff, _intent_for_upload
from videoclip.settings import Settings
from videoclip.storage import MediaStore


@pytest.fixture(scope="session")
def valid_mp4(tmp_path_factory: pytest.TempPathFactory, test_settings: Settings) -> Path:
    directory = tmp_path_factory.mktemp("media")
    destination = directory / "verified-sample.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        ffmpeg_name = "ffmpeg.exe" if __import__("os").name == "nt" else "ffmpeg"
        candidate = Path(test_settings.ffprobe_path).with_name(ffmpeg_name)
        if not candidate.exists():
            pytest.skip("ffmpeg is required to make the small validated upload fixture")
        ffmpeg = str(candidate)
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=64x64:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "1.4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=45)
    return destination


def _client(app):
    return TestClient(app)


def _new_account(client: TestClient, username: str) -> None:
    assert register(client, username).status_code == 201
    assert login(client, username).status_code == 200


def _upload(client: TestClient, media: Path, *, headers: dict[str, str] | None = None):
    data = media.read_bytes()
    csrf_token = csrf(client)
    upload_headers = mutate_headers(csrf_token)
    upload_headers.update({"X-Video-Filename": "archive.txt", "Content-Type": "text/plain"})
    if headers:
        upload_headers.update(headers)
    return client.post("/videos", content=data, headers=upload_headers), data


def test_raw_stream_upload_is_owner_scoped_and_supports_head_and_ranges(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path, valid_mp4: Path
) -> None:
    store = MediaStore(tmp_path / "persistent-media")
    app = create_app(test_settings, session_factory, store)
    with _client(app) as owner, _client(app) as other:
        _new_account(owner, "upload-owner")
        _new_account(other, "upload-other")
        data = valid_mp4.read_bytes()
        middle = len(data) // 3
        content_chunks = iter((data[:middle], data[middle : 2 * middle], data[2 * middle :]))
        token = csrf(owner)
        response = owner.post(
            "/videos",
            content=content_chunks,
            headers={
                **mutate_headers(token),
                "X-Video-Filename": "course-not-a-video.txt",
                "Content-Type": "text/plain",
                "Content-Length": "1",
            },
        )
        assert response.status_code == 201, response.text
        video = response.json()["video"]
        video_id = video["id"]
        assert video["display_name"] == "course-not-a-video.txt"
        assert video["byte_count"] == len(data)
        assert video["content_sha256"] == hashlib.sha256(data).hexdigest()
        assert video["duration_seconds"] > 0 and video["duration_seconds"] <= 1200
        assert video["media_info"]["video_codec"] == "h264"
        assert video["media_info"]["audio_codec"] == "aac"
        assert "storage_name" not in response.text and str(store.root) not in response.text

        listing = owner.get("/videos")
        assert listing.status_code == 200 and listing.json()["items"][0]["id"] == video_id
        assert owner.get(f"/videos/{video_id}").json()["analysis_task_exists"] is False
        assert other.get("/videos").json()["items"] == []
        assert other.get(f"/videos/{video_id}").status_code == 404
        assert other.get(f"/videos/{video_id}/content").status_code == 404

        head = owner.head(f"/videos/{video_id}/content")
        assert head.status_code == 200 and head.headers["content-length"] == str(len(data))
        ranged = owner.get(f"/videos/{video_id}/content", headers={"Range": "bytes=5-20"})
        assert ranged.status_code == 206 and ranged.content == data[5:21]
        assert ranged.headers["content-range"] == f"bytes 5-20/{len(data)}"
        suffix = owner.get(f"/videos/{video_id}/content", headers={"Range": "bytes=-8"})
        assert suffix.status_code == 206 and suffix.content == data[-8:]
        assert owner.get(f"/videos/{video_id}/content", headers={"Range": "bytes=999999-"}).status_code == 416
        assert owner.get(f"/videos/{video_id}/content", headers={"Range": "bytes=0-1,4-5"}).status_code == 416

        with session_factory() as session:
            stored_video = session.get(Video, video_id)
            assert stored_video is not None
            target = store.target_path(stored_video.storage_name)
        assert owner.delete(f"/videos/{video_id}", headers=mutate_headers(csrf(owner))).status_code == 204
        assert owner.get(f"/videos/{video_id}/content").status_code == 404
        assert owner.get(f"/videos/{video_id}").status_code == 404
        assert not target.exists()
        with session_factory() as session:
            assert session.get(Video, video_id) is None
            assert session.scalar(select(UploadHandoff).where(UploadHandoff.state == "complete")) is None
            assert session.scalars(select(CleanupIntent)).all() == []


def test_upload_ignores_declared_content_length_and_persists_across_app_restart(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path, valid_mp4: Path
) -> None:
    store = MediaStore(tmp_path / "persistent-media")
    app = create_app(test_settings, session_factory, store)
    with _client(app) as client:
        _new_account(client, "restart-owner")
        response, data = _upload(client, valid_mp4, headers={"Content-Length": "999999999999"})
        assert response.status_code == 201, response.text
        video_id = response.json()["video"]["id"]
        response_headers = response.headers
        assert response_headers["content-type"].startswith("application/json")
    restarted = create_app(test_settings, session_factory, store)
    with _client(restarted) as client:
        assert client.get("/videos").status_code == 401
        assert login(client, "restart-owner").status_code == 200
        listing = client.get("/videos")
        assert listing.status_code == 200
        assert listing.json()["items"][0]["id"] == video_id
        assert client.get(f"/videos/{video_id}/content").content == data


def test_deletion_gate_and_retryable_storage_failure(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    valid_mp4: Path,
    monkeypatch,
) -> None:
    store = MediaStore(tmp_path / "persistent-media")
    app = create_app(test_settings, session_factory, store)
    with _client(app) as client:
        _new_account(client, "delete-owner")
        response, _ = _upload(client, valid_mp4)
        assert response.status_code == 201
        video = response.json()["video"]
        video_id = video["id"]
        with session_factory() as session:
            stored_video = session.get(Video, video_id)
            assert stored_video is not None
            target = store.target_path(stored_video.storage_name)

        def fail_remove(_: str) -> None:
            raise OSError("simulated file-system failure")

        monkeypatch.setattr(store, "remove_media", fail_remove)
        failed = client.delete(f"/videos/{video_id}", headers=mutate_headers(csrf(client)))
        assert failed.status_code == 202
        details = client.get(f"/videos/{video_id}").json()
        assert details["status"] == "cleanup_failed"
        assert details["deletion_error"] == "STORAGE_OPERATION_FAILED"
        assert client.get(f"/videos/{video_id}/content").status_code == 404
        with session_factory() as session:
            assert session.scalar(select(CleanupIntent).where(CleanupIntent.kind == "video")) is not None
        assert target.exists()

        monkeypatch.setattr(store, "remove_media", MediaStore.remove_media.__get__(store, MediaStore))
        retried = client.delete(f"/videos/{video_id}", headers=mutate_headers(csrf(client)))
        assert retried.status_code == 204
        assert not target.exists()
        assert client.get(f"/videos/{video_id}").status_code == 404


def test_video_with_started_analysis_cannot_use_direct_delete(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path, valid_mp4: Path
) -> None:
    store = MediaStore(tmp_path / "persistent-media")
    app = create_app(test_settings, session_factory, store)
    with _client(app) as client:
        _new_account(client, "analysis-owner")
        response, _ = _upload(client, valid_mp4)
        video_id = response.json()["video"]["id"]
        with session_factory.begin() as session:
            video = session.get(Video, video_id)
            assert video is not None
            video.analysis_task_created_at = datetime.now(UTC)
        blocked = client.delete(f"/videos/{video_id}", headers=mutate_headers(csrf(client)))
        assert blocked.status_code == 409
        assert client.get(f"/videos/{video_id}/content").status_code == 200


def test_delete_waits_for_live_upload_writer_lease(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path, valid_mp4: Path
) -> None:
    store = MediaStore(tmp_path / "lease-media")
    app = create_app(test_settings, session_factory, store)
    with _client(app) as client:
        _new_account(client, "lease-owner")
        response, _ = _upload(client, valid_mp4)
        video_id = response.json()["video"]["id"]
        with session_factory() as session:
            video = session.get(Video, video_id)
            assert video is not None
            upload_id = video.upload_id
            target = store.target_path(video.storage_name)
        active_writer = store.acquire_lease(upload_id)
        assert active_writer is not None
        pending = client.delete(f"/videos/{video_id}", headers=mutate_headers(csrf(client)))
        assert pending.status_code == 202
        assert target.exists()
        active_writer.release()
        retried = client.delete(f"/videos/{video_id}", headers=mutate_headers(csrf(client)))
        assert retried.status_code == 204
        assert not target.exists()


def test_cleanup_deadline_returns_pending_while_safe_cleanup_continues(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    valid_mp4: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bounded = test_settings.model_copy(update={"cleanup_timeout_seconds": 0.05})
    store = MediaStore(tmp_path / "bounded-cleanup-media")
    app = create_app(bounded, session_factory, store)
    original_remove = store.remove_media
    with _client(app) as client:
        _new_account(client, "slow-cleanup")
        response, _ = _upload(client, valid_mp4)
        video_id = response.json()["video"]["id"]

        def slow_remove(storage_name: str) -> None:
            sleep(0.25)
            original_remove(storage_name)

        monkeypatch.setattr(store, "remove_media", slow_remove)
        started = monotonic()
        pending = client.delete(f"/videos/{video_id}", headers=mutate_headers(csrf(client)))
        elapsed = monotonic() - started
        assert pending.status_code == 202
        assert elapsed < 0.2
        deadline = monotonic() + 2
        while client.get(f"/videos/{video_id}").status_code != 404:
            if monotonic() >= deadline:
                pytest.fail("bounded background cleanup did not finish")
            sleep(0.05)


@pytest.mark.parametrize("body", [b"", b"not an MP4 media file"])
def test_empty_and_invalid_media_uploads_are_rejected_and_removed(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    body: bytes,
) -> None:
    store = MediaStore(tmp_path / "rejected-media")
    app = create_app(test_settings, session_factory, store)
    with _client(app) as client:
        _new_account(client, "invalid-media")
        response = client.post(
            "/videos",
            content=body,
            headers={**mutate_headers(csrf(client)), "Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 422
        assert client.get("/videos").json()["items"] == []
        assert list(store.incoming.glob("*.part")) == []
        with session_factory() as session:
            assert session.scalars(select(Video)).all() == []
            assert session.scalars(select(UploadHandoff)).all() == []
            assert session.scalars(select(CleanupIntent)).all() == []


def test_failed_chunk_write_leaves_no_video_and_cleans_part(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    valid_mp4: Path,
    monkeypatch,
) -> None:
    store = MediaStore(tmp_path / "persistent-media")
    app = create_app(test_settings, session_factory, store)
    with _client(app) as client:
        _new_account(client, "write-failure")

        def fail_write(_: object, __: bytes) -> None:
            raise OSError("simulated write failure")

        monkeypatch.setattr(store, "write_chunk", fail_write)
        response = client.post(
            "/videos",
            content=valid_mp4.read_bytes(),
            headers={**mutate_headers(csrf(client)), "Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 507
        assert client.get("/videos").json()["items"] == []
        assert list(store.incoming.glob("*.part")) == []
        with session_factory() as session:
            assert session.scalars(select(UploadHandoff)).all() == []
            assert session.scalars(select(CleanupIntent)).all() == []


def test_disconnect_persists_and_cleans_upload_handoff(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path
) -> None:
    import asyncio

    from starlette.requests import ClientDisconnect

    from videoclip.services import receive_video_upload

    store = MediaStore(tmp_path / "disconnect-media")
    app = create_app(test_settings, session_factory, store)
    with TestClient(app) as client:
        _new_account(client, "disconnect-owner")
        owner_id = client.get("/auth/me").json()["id"]
    messages = iter(
        (
            {"type": "http.request", "body": b"incomplete", "more_body": True},
            {"type": "http.disconnect"},
        )
    )

    async def receive():
        return next(messages)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/videos",
        "raw_path": b"/videos",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"x-video-filename", b"disconnected.mp4")],
        "client": ("127.0.0.1", 43210),
        "server": ("127.0.0.1", 58001),
    }
    request = __import__("starlette.requests", fromlist=["Request"]).Request(scope, receive)
    with pytest.raises(ClientDisconnect):
        asyncio.run(receive_video_upload(request, session_factory, test_settings, store, owner_id))
    assert list(store.incoming.glob("*.part")) == []
    with session_factory() as session:
        assert session.scalars(select(UploadHandoff)).all() == []
        assert session.scalars(select(Video)).all() == []


def test_registration_failure_after_file_rename_is_recovered(
    session_factory: sessionmaker[Session],
    test_settings: Settings,
    tmp_path: Path,
    valid_mp4: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlalchemy

    import videoclip.services as services

    store = MediaStore(tmp_path / "promotion-failure-media")
    app = create_app(test_settings, session_factory, store)
    with _client(app) as client:
        _new_account(client, "promotion-failure")

        def fail_registration(*_: object, **__: object) -> Video:
            raise sqlalchemy.exc.OperationalError(
                "registration", {}, RuntimeError("simulated commit failure")
            )

        monkeypatch.setattr(services, "_complete_upload", fail_registration)
        response = client.post(
            "/videos",
            content=valid_mp4.read_bytes(),
            headers={**mutate_headers(csrf(client)), "Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 503
        with session_factory() as session:
            pending = session.scalar(select(UploadHandoff).where(UploadHandoff.state == "promoting"))
            assert pending is not None
            upload_id = pending.id
            assert session.scalar(select(Video).where(Video.upload_id == upload_id)) is None
            assert store.target_path(pending.target_name).exists()

        monkeypatch.undo()
        recover_interrupted_work(session_factory, test_settings, store)
        with session_factory() as session:
            assert session.scalar(select(Video).where(Video.upload_id == upload_id)) is not None
            assert (
                session.scalar(select(UploadHandoff).where(UploadHandoff.id == upload_id)).state == "complete"
            )


def test_recovery_waits_for_writer_lease_before_removing_partial_upload(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path
) -> None:
    store = MediaStore(tmp_path / "recovery-media")
    store.prepare()
    with session_factory() as session:
        owner = session.scalar(select(User).where(User.username == "recovery-owner"))
        if owner is None:
            owner = User(username="recovery-owner", password_hash="$argon2id$test")
            session.add(owner)
            session.commit()
        owner_id = owner.id
    upload_id = uuid4()
    lease = store.acquire_lease(upload_id)
    assert lease is not None
    part = store.part_path(upload_id)
    part.write_bytes(b"unfinished")
    _create_handoff(session_factory, owner_id, upload_id, uuid4(), "unfinished.mp4")

    recover_interrupted_work(session_factory, test_settings, store)
    assert part.exists()
    with session_factory() as session:
        assert session.get(UploadHandoff, upload_id) is not None

    lease.release()
    recover_interrupted_work(session_factory, test_settings, store)
    assert not part.exists()
    with session_factory() as session:
        assert session.get(UploadHandoff, upload_id) is None


def test_recovery_finishes_persisted_promotion_after_file_rename(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path
) -> None:
    import hashlib

    store = MediaStore(tmp_path / "recovery-media")
    store.prepare()
    with session_factory() as session:
        owner = session.scalar(select(User).where(User.username == "promotion-owner"))
        if owner is None:
            owner = User(username="promotion-owner", password_hash="$argon2id$test")
            session.add(owner)
            session.commit()
        owner_id = owner.id
    upload_id = uuid4()
    writer_id = uuid4()
    _create_handoff(session_factory, owner_id, upload_id, writer_id, "course.mp4")
    body = b"a previously probed, verified source"
    digest = hashlib.sha256(body).hexdigest()
    with session_factory.begin() as session:
        upload = session.get(UploadHandoff, upload_id)
        assert upload is not None
        upload.state = "promoting"
        upload.byte_count = len(body)
        upload.content_sha256 = digest
        upload.duration_seconds = 12.5
        upload.media_info = {"container": "mp4", "timeline_start_seconds": 0}
        _intent_for_upload(session, owner_id, upload_id, "promotion_pending")
    target = store.target_path(upload_id.hex)
    target.write_bytes(body)

    recover_interrupted_work(session_factory, test_settings, store)
    with session_factory() as session:
        video = session.scalar(select(Video).where(Video.upload_id == upload_id))
        upload = session.get(UploadHandoff, upload_id)
        assert video is not None and video.content_sha256 == digest
        assert upload is not None and upload.state == "complete"
