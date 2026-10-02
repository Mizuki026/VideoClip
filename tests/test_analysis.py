from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from kombu.exceptions import EncodeError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from videoclip.analysis import (
    build_configuration_snapshot,
    canonical_sha256,
    submit_analysis_task,
)
from videoclip.analysis_worker import (
    AnalysisFailure,
    ExecutionLease,
    _claim_attempt,
    _persist_transcript,
    _read_asr_result,
    _run_process,
    _set_stage,
    _start_segment_request,
    build_manifest,
)
from videoclip.asr_client import AsrCallError, transcribe
from videoclip.celery_app import celery_app
from videoclip.main import create_app
from videoclip.models import (
    AnalysisAttempt,
    AnalysisExecution,
    AnalysisTask,
    TranscriptSegment,
    UploadHandoff,
    Video,
)
from videoclip.schemas import AnalysisTaskResponse
from videoclip.services import delete_video
from videoclip.settings import Settings
from videoclip.storage import MediaStore

PASSWORD = "correct horse battery staple 42"


def _video(
    factory: sessionmaker[Session], store: MediaStore, owner_id: uuid.UUID, duration: float = 61.25
) -> uuid.UUID:
    upload_id = uuid.uuid4()
    storage_name = uuid.uuid4().hex
    content = b"fixture-media"
    upload = UploadHandoff(
        id=upload_id,
        owner_id=owner_id,
        writer_id=uuid.uuid4(),
        part_name=f"{upload_id.hex}.part",
        target_name=storage_name,
        display_name="fixture.mp4",
        state="complete",
        byte_count=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        duration_seconds=duration,
        media_info={
            "container": "mp4",
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "audio_codec": "aac",
            "timeline_start_seconds": 0.0,
            "streams": [
                {"index": 0, "type": "video", "codec": "h264", "start_time": 0, "time_base": "1/30"},
                {"index": 1, "type": "audio", "codec": "aac", "start_time": 0, "time_base": "1/48000"},
            ],
        },
    )
    video = Video(
        id=uuid.uuid4(),
        owner_id=owner_id,
        upload_id=upload_id,
        display_name="fixture.mp4",
        storage_name=storage_name,
        byte_count=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        duration_seconds=duration,
        media_info=upload.media_info,
        status="ready",
    )
    with factory.begin() as session:
        session.add(upload)
        session.add(video)
    store.prepare()
    store.target_path(storage_name).write_bytes(content)
    return video.id


def _register(client: TestClient, username: str) -> uuid.UUID:
    from tests.helpers import login, register

    response = register(client, username, PASSWORD)
    assert response.status_code == 201
    login_response = login(client, username, PASSWORD)
    assert login_response.status_code == 200
    return uuid.UUID(response.json()["id"])


def test_submit_freezes_configuration_and_is_idempotent(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    published: list[str] = []
    app = create_app(
        test_settings,
        session_factory,
        MediaStore(test_settings.app_data_root),
        run_recovery=False,
        task_publisher=lambda task_id: published.append(task_id),
    )
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-owner")
        video_id = _video(session_factory, app.state.store, owner_id)
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        response = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert response.status_code == 202
        first = AnalysisTaskResponse.model_validate(response.json())
        assert first.status == "queued"
        assert first.delivery_status == "sent"
        assert first.execution_state == "not_started"
        assert first.execution_stop_confirmed is None
        assert first.summary_status == "not_started"
        assert first.asr_model == test_settings.asr_model
        assert first.asr_request_count == 0
        assert first.expected_segments == 0
        assert len(published) == 1 and published[0] == str(first.id)

        repeated = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert repeated.status_code == 202
        assert repeated.json()["id"] == str(first.id)
        assert len(published) == 1

        transcript = client.get(f"/videos/{video_id}/transcript")
        assert transcript.status_code == 200
        assert transcript.json() == {
            "task_id": str(first.id),
            "complete": False,
            "summary_status": "not_started",
            "items": [],
        }

        with session_factory() as session:
            video = session.get(Video, video_id)
            task = session.get(AnalysisTask, first.id)
            attempt = session.get(AnalysisAttempt, first.attempt.id)
            assert video is not None and video.analysis_task_created_at is not None
            assert video.current_analysis_task_id == first.id
            assert task is not None and task.status == "queued"
            assert attempt is not None and attempt.claim_count == 0
            assert attempt.config_sha256 == canonical_sha256(attempt.config_snapshot)
            assert attempt.config_snapshot["source"]["fingerprint"] == attempt.source_fingerprint
            assert attempt.config_snapshot["asr"]["secret_reference"] == "environment:SILICONFLOW_API_KEY"
            assert "api_key" not in attempt.config_snapshot["asr"]

        removed = client.delete(f"/videos/{video_id}", headers=headers)
        assert removed.status_code == 409


def test_submission_and_task_reads_are_owner_scoped(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    app = create_app(test_settings, session_factory, run_recovery=False, task_publisher=lambda _: None)
    with TestClient(app) as owner, TestClient(app) as stranger:
        owner_id = _register(owner, "analysis-owner-two")
        stranger_id = _register(stranger, "analysis-stranger")
        video_id = _video(session_factory, app.state.store, owner_id)
        from tests.helpers import csrf, mutate_headers

        created = owner.post(f"/videos/{video_id}/tasks", headers=mutate_headers(csrf(owner)))
        assert created.status_code == 202
        task_id = created.json()["id"]
        assert stranger_id != owner_id
        assert stranger.get(f"/videos/{video_id}/tasks/{task_id}").status_code == 404
        assert stranger.get(f"/videos/{video_id}/transcript").status_code == 404
        stranger_headers = mutate_headers(csrf(stranger))
        assert stranger.post(f"/videos/{video_id}/tasks", headers=stranger_headers).status_code == 404


def test_broker_error_is_unknown_and_does_not_republish_on_duplicate(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    calls = 0

    def broker_failure(_: str) -> None:
        nonlocal calls
        calls += 1
        raise OSError("broker confirmation unavailable")

    app = create_app(test_settings, session_factory, run_recovery=False, task_publisher=broker_failure)
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-broker-error")
        video_id = _video(session_factory, app.state.store, owner_id)
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        first = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert first.status_code == 202
        assert first.json()["delivery_status"] == "unknown"
        assert first.json()["delivery_error_code"] == "BROKER_CONFIRMATION_UNKNOWN"
        assert first.json()["status"] == "queued"
        assert first.json()["execution_state"] == "not_started"
        duplicate = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert duplicate.status_code == 202
        assert duplicate.json()["id"] == first.json()["id"]
        assert calls == 1


def test_proven_local_payload_failure_is_failed_without_task_failure(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    calls = 0

    def serialization_failure(_: str) -> None:
        nonlocal calls
        calls += 1
        raise EncodeError("injected local serialization failure")

    app = create_app(test_settings, session_factory, run_recovery=False, task_publisher=serialization_failure)
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-encode-error")
        video_id = _video(session_factory, app.state.store, owner_id)
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        first = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert first.status_code == 202
        assert first.json()["delivery_status"] == "failed"
        assert first.json()["delivery_error_code"] == "BROKER_PAYLOAD_REJECTED"
        assert first.json()["status"] == "queued"
        duplicate = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert duplicate.status_code == 202
        assert duplicate.json()["delivery_status"] == "failed"
        assert calls == 1


def test_database_commit_before_publish_exit_leaves_pending_delivery(
    test_settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import videoclip.main as main

    published: list[str] = []

    def fail_before_publish(*_args: Any, **_kwargs: Any) -> None:
        raise SQLAlchemyError("injected publish-start persistence failure")

    monkeypatch.setattr(main, "mark_publish_started", fail_before_publish)
    app = create_app(test_settings, session_factory, run_recovery=False, task_publisher=published.append)
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-pending-delivery")
        video_id = _video(session_factory, app.state.store, owner_id)
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        first = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert first.status_code == 202
        assert first.json()["delivery_status"] == "pending"
        duplicate = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert duplicate.status_code == 202
        assert duplicate.json()["delivery_status"] == "pending"
        assert published == []


def test_successful_publish_with_lost_confirmation_stays_pending(
    test_settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import videoclip.main as main

    published: list[str] = []
    original_set_status = main.set_delivery_status

    def lose_sent_confirmation(
        factory: sessionmaker[Session], attempt_id: uuid.UUID, state: str, code: str | None
    ) -> None:
        if state == "sent":
            raise SQLAlchemyError("injected lost sent confirmation")
        original_set_status(factory, attempt_id, state, code)  # pragma: no cover

    monkeypatch.setattr(main, "set_delivery_status", lose_sent_confirmation)
    app = create_app(test_settings, session_factory, run_recovery=False, task_publisher=published.append)
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-lost-confirmation")
        video_id = _video(session_factory, app.state.store, owner_id)
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        first = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert first.status_code == 202
        assert first.json()["delivery_status"] == "pending"
        duplicate = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert duplicate.status_code == 202
        assert duplicate.json()["delivery_status"] == "pending"
        assert len(published) == 1


def test_broker_error_cannot_overwrite_claimed_execution(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)

    def claim_then_fail(task_id_value: str) -> None:
        lease = ExecutionLease.acquire(test_engine)
        assert lease is not None
        assert _claim_attempt(lease, uuid.UUID(task_id_value), "publisher-race-worker") is not None
        lease.release()
        raise OSError("broker acknowledgement was lost")

    app = create_app(
        test_settings, session_factory, store, run_recovery=False, task_publisher=claim_then_fail
    )
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-publish-race")
        video_id = _video(session_factory, store, owner_id)
        from tests.helpers import csrf, mutate_headers

        response = client.post(f"/videos/{video_id}/tasks", headers=mutate_headers(csrf(client)))
        assert response.status_code == 202
        assert response.json()["status"] == "running"
        assert response.json()["delivery_status"] == "unknown"
        assert response.json()["execution_state"] == "stop_unconfirmed"


def test_database_lock_loss_stops_local_process_and_blocks_another_claim(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import videoclip.analysis_worker as worker

    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-db-lock-loss")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    lease = ExecutionLease.acquire(test_engine)
    assert lease is not None
    claim = _claim_attempt(lease, submitted.task_id, "test-worker-lock-loss")
    assert claim is not None
    maker = sessionmaker(bind=lease.connection, autoflush=False, expire_on_commit=False)
    spawned: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen
    original_progress_check = worker._progress_check
    checks = 0

    def capture_process(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    def lose_connection_after_start(
        bound_maker: sessionmaker[Session], bound_lease: ExecutionLease, bound_claim: Any
    ) -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            with test_engine.connect() as killer:
                killer.execute(
                    text("SELECT pg_terminate_backend(:backend_pid)"),
                    {"backend_pid": bound_lease.backend_pid},
                )
                killer.commit()
        original_progress_check(bound_maker, bound_lease, bound_claim)

    monkeypatch.setattr(subprocess, "Popen", capture_process)
    monkeypatch.setattr(worker, "_progress_check", lose_connection_after_start)
    monkeypatch.setattr(worker, "CHECK_INTERVAL_SECONDS", 0.01)
    with pytest.raises(AnalysisFailure, match="EXECUTION_DATABASE_LOCK_LOST"):
        _run_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=10,
            grace_seconds=1,
            maker=maker,
            lease=lease,
            claim=claim,
            attempt_deadline=time.monotonic() + 30,
        )
    assert len(spawned) == 1 and spawned[0].poll() is not None
    lease.release()

    next_lease = ExecutionLease.acquire(test_engine)
    assert next_lease is not None
    assert _claim_attempt(next_lease, submitted.task_id, "test-worker-after-lock-loss") is None
    next_lease.release()
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        attempt = session.get(AnalysisAttempt, submitted.attempt_id)
        execution = session.scalar(
            select(AnalysisExecution).where(AnalysisExecution.attempt_id == submitted.attempt_id)
        )
        assert task is not None and task.status == "running"
        assert attempt is not None and attempt.claim_count == 1 and attempt.stop_confirmed_at is None
        assert execution is not None and execution.stop_confirmed_at is None


def test_invalidated_video_cannot_advance_the_claimed_task(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-invalidated-video")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    lease = ExecutionLease.acquire(test_engine)
    assert lease is not None
    claim = _claim_attempt(lease, submitted.task_id, "test-worker-invalidated-video")
    assert claim is not None
    maker = sessionmaker(bind=lease.connection, autoflush=False, expire_on_commit=False)
    with session_factory.begin() as session:
        video = session.get(Video, video_id)
        assert video is not None
        video.status = "deleting"
    with pytest.raises(AnalysisFailure, match="EXECUTION_OWNERSHIP_LOST"):
        _set_stage(maker, lease, claim, "transcribing")
    lease.release()
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None and task.status == "running" and task.stage == "validating_source"


def test_configuration_and_source_fingerprints_are_stable(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-fingerprint")
        video_id = _video(session_factory, client.app.state.store, owner_id)
        with session_factory() as session:
            video = session.get(Video, video_id)
            assert video is not None
            first = build_configuration_snapshot(video, test_settings)
            changed = test_settings.model_copy(update={"asr_model": "a-new-default-model"})
            second = build_configuration_snapshot(video, changed)
        assert first["source"]["fingerprint"] == second["source"]["fingerprint"]
        assert first["asr"]["model"] != second["asr"]["model"]
        assert canonical_sha256(first) != canonical_sha256(second)


def test_attempt_can_be_claimed_only_once_even_after_lock_release(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-claim-once")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    first_lease = ExecutionLease.acquire(test_engine)
    assert first_lease is not None
    first_claim = _claim_attempt(first_lease, submitted.task_id, "test-worker-one")
    assert first_claim is not None
    first_lease.release()

    second_lease = ExecutionLease.acquire(test_engine)
    assert second_lease is not None
    duplicate_claim = _claim_attempt(second_lease, submitted.task_id, "test-worker-two")
    assert duplicate_claim is None
    second_lease.release()
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        attempt = session.get(AnalysisAttempt, submitted.attempt_id)
        executions = session.scalar(
            select(func.count(AnalysisExecution.id)).where(AnalysisExecution.stop_confirmed_at.is_(None))
        )
        assert task is not None and task.status == "running"
        assert attempt is not None and attempt.claim_count == 1
        assert executions == 1


def test_global_execution_slot_blocks_a_competing_video(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-global-slot")
        video_a = _video(session_factory, store, owner_id)
        video_b = _video(session_factory, store, owner_id)
    submitted_a = submit_analysis_task(session_factory, owner_id, video_a, test_settings)
    submitted_b = submit_analysis_task(session_factory, owner_id, video_b, test_settings)
    first_lease = ExecutionLease.acquire(test_engine)
    assert first_lease is not None
    assert _claim_attempt(first_lease, submitted_a.task_id, "test-worker-one") is not None
    first_lease.release()

    second_lease = ExecutionLease.acquire(test_engine)
    assert second_lease is not None
    assert _claim_attempt(second_lease, submitted_b.task_id, "test-worker-two") is None
    second_lease.release()
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted_b.task_id)
        assert task is not None and task.status == "queued"
        assert (
            session.scalar(
                select(func.count(AnalysisExecution.id)).where(AnalysisExecution.stop_confirmed_at.is_(None))
            )
            == 1
        )


def test_request_started_before_checkpoint_failure_prevents_reclaim(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-checkpoint-failure")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    lease = ExecutionLease.acquire(test_engine)
    assert lease is not None
    claim = _claim_attempt(lease, submitted.task_id, "test-worker-checkpoint")
    assert claim is not None
    with session_factory.begin() as session:
        session.add(
            TranscriptSegment(
                attempt_id=submitted.attempt_id,
                segment_number=0,
                audio_start_seconds=0,
                audio_end_seconds=30,
                actual_audio_seconds=30,
                video_start_seconds=0,
                video_end_seconds=30,
                audio_sha256="a" * 64,
                request_fingerprint="b" * 64,
                status="expected",
                request_count=0,
            )
        )
    maker = sessionmaker(bind=lease.connection, autoflush=False, expire_on_commit=False)
    _start_segment_request(maker, lease, claim, 0)
    with test_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION test_reject_transcript_checkpoint() RETURNS trigger AS $$ "
                "BEGIN IF NEW.text IS NOT NULL THEN RAISE EXCEPTION 'injected checkpoint failure'; "
                "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER test_reject_transcript_checkpoint BEFORE UPDATE ON transcript_segments "
                "FOR EACH ROW EXECUTE FUNCTION test_reject_transcript_checkpoint()"
            )
        )
    try:
        with pytest.raises(SQLAlchemyError):
            _persist_transcript(maker, lease, claim, 0, "model returned this, but it is not a checkpoint", {})
    finally:
        with test_engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER IF EXISTS test_reject_transcript_checkpoint ON transcript_segments")
            )
            connection.execute(text("DROP FUNCTION IF EXISTS test_reject_transcript_checkpoint()"))
    lease.release()

    with session_factory() as session:
        segment = session.scalar(
            select(TranscriptSegment).where(
                TranscriptSegment.attempt_id == submitted.attempt_id,
                TranscriptSegment.segment_number == 0,
            )
        )
        assert segment is not None
        assert segment.request_count == 1
        assert segment.status == "expected" and segment.text is None
    next_lease = ExecutionLease.acquire(test_engine)
    assert next_lease is not None
    assert _claim_attempt(next_lease, submitted.task_id, "test-worker-replay") is None
    next_lease.release()


def test_stale_execution_id_cannot_write_a_transcript_request(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-stale-execution")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    with session_factory.begin() as session:
        session.add(
            TranscriptSegment(
                attempt_id=submitted.attempt_id,
                segment_number=0,
                audio_start_seconds=0,
                audio_end_seconds=30,
                actual_audio_seconds=30,
                video_start_seconds=0,
                video_end_seconds=30,
                audio_sha256="a" * 64,
                request_fingerprint="b" * 64,
                status="expected",
                request_count=0,
            )
        )
    lease = ExecutionLease.acquire(test_engine)
    assert lease is not None
    claim = _claim_attempt(lease, submitted.task_id, "test-worker-stale")
    assert claim is not None
    stale = claim.__class__(
        task_id=claim.task_id,
        attempt_id=claim.attempt_id,
        execution_id=uuid.uuid4(),
        video_id=claim.video_id,
        storage_name=claim.storage_name,
        snapshot=claim.snapshot,
        work_directory_name=claim.work_directory_name,
    )
    maker = sessionmaker(bind=lease.connection, autoflush=False, expire_on_commit=False)
    with pytest.raises(AnalysisFailure, match="EXECUTION_OWNERSHIP_LOST"):
        _start_segment_request(maker, lease, stale, 0)
    lease.release()
    with session_factory() as session:
        segment = session.scalar(
            select(TranscriptSegment).where(
                TranscriptSegment.attempt_id == submitted.attempt_id,
                TranscriptSegment.segment_number == 0,
            )
        )
        assert segment is not None and segment.request_count == 0


def test_attempt_configuration_is_immutable_in_postgres(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-immutable-config")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    with pytest.raises(SQLAlchemyError):
        with test_engine.begin() as connection:
            connection.execute(
                text("UPDATE analysis_attempts SET config_snapshot = '{}'::jsonb WHERE id = :attempt_id"),
                {"attempt_id": submitted.attempt_id},
            )
    with session_factory() as session:
        attempt = session.get(AnalysisAttempt, submitted.attempt_id)
        assert attempt is not None and attempt.config_snapshot["asr"]["model"] == test_settings.asr_model


def test_local_process_timeout_stops_and_reaps_the_child_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import videoclip.analysis_worker as worker

    spawned: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen

    def capture_process(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", capture_process)
    monkeypatch.setattr(worker, "_progress_check", lambda *_args: None)
    with pytest.raises(AnalysisFailure, match="LOCAL_PROCESS_TIMEOUT"):
        _run_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.2,
            grace_seconds=1,
            maker=None,  # type: ignore[arg-type]
            lease=None,  # type: ignore[arg-type]
            claim=None,  # type: ignore[arg-type]
            attempt_deadline=time.monotonic() + 10,
        )
    assert len(spawned) == 1 and spawned[0].poll() is not None


def test_submit_and_delete_race_has_one_winner(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-delete-race")
        video_id = _video(session_factory, store, owner_id)
    with session_factory() as session:
        initial_video = session.get(Video, video_id)
        assert initial_video is not None
        storage_name = initial_video.storage_name
    start = threading.Barrier(2)

    def submit() -> str:
        start.wait()
        try:
            submit_analysis_task(session_factory, owner_id, video_id, test_settings)
            return "submitted"
        except HTTPException as exc:
            assert exc.status_code in {404, 409}
            return "submit_rejected"

    def delete() -> str:
        start.wait()
        try:
            return (
                "deleted"
                if delete_video(session_factory, store, owner_id, video_id) == 204
                else "delete_pending"
            )
        except HTTPException as exc:
            assert exc.status_code == 409
            return "delete_rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(submit), pool.submit(delete)]
        results = [future.result(timeout=20) for future in outcomes]
    with session_factory() as session:
        video = session.get(Video, video_id)
        task_count = session.scalar(
            select(func.count(AnalysisTask.id)).where(AnalysisTask.video_id == video_id)
        )
        if video is None:
            assert task_count == 0
            assert not store.target_path(storage_name).exists()
        else:
            assert video.status == "ready"
            assert video.current_analysis_task_id is not None
            assert task_count == 1
            assert store.target_path(video.storage_name).exists()
    assert {"submitted", "delete_rejected"} == set(results) or "deleted" in results


def test_manifest_uses_actual_last_segment_duration() -> None:
    entries = build_manifest(
        pcm_bytes=62 * 16_000 * 2,
        duration_seconds=62,
        sample_rate=16_000,
        channels=1,
        segment_seconds=30,
    )
    assert len(entries) == 3
    assert [item.actual_audio_seconds for item in entries] == [30.0, 30.0, 2.0]
    assert [(item.video_start_seconds, item.video_end_seconds) for item in entries] == [
        (0.0, 30.0),
        (30.0, 60.0),
        (60.0, 62.0),
    ]
    assert entries == build_manifest(
        pcm_bytes=62 * 16_000 * 2,
        duration_seconds=62,
        sample_rate=16_000,
        channels=1,
        segment_seconds=30,
    )


def test_asr_adapter_sends_bounded_wav_request_and_saves_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "segment.wav"
    audio.write_bytes(b"wave-data")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "private-test-key")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer private-test-key"
        return httpx.Response(
            200,
            json={"text": "第一段转写"},
            headers={"x-siliconcloud-trace-id": "trace-test"},
        )

    text_value, metadata = transcribe(
        audio,
        base_url="https://api.siliconflow.cn/v1",
        model="FunAudioLLM/SenseVoiceSmall",
        total_timeout=3,
        connect_timeout=1,
        read_timeout=1,
        maximum_response_bytes=256,
        parameters={},
        transport=httpx.MockTransport(handler),
    )
    assert text_value == "第一段转写"
    assert metadata["model"] == "FunAudioLLM/SenseVoiceSmall"
    assert metadata["http_status"] == 200
    assert metadata["trace_id"] == "trace-test"
    assert len(seen) == 1 and seen[0].url.path == "/v1/audio/transcriptions"


def test_asr_adapter_rejects_oversized_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "segment.wav"
    audio.write_bytes(b"wave-data")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "private-test-key")
    with pytest.raises(AsrCallError, match="ASR_RESPONSE_TOO_LARGE"):
        transcribe(
            audio,
            base_url="https://api.siliconflow.cn/v1",
            model="FunAudioLLM/SenseVoiceSmall",
            total_timeout=3,
            connect_timeout=1,
            read_timeout=1,
            maximum_response_bytes=4,
            parameters={},
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"text": "too large"})),
        )


def test_asr_adapter_preserves_bounded_provider_error_code(tmp_path: Path) -> None:
    response = tmp_path / "response.json"
    response.write_text('{"error_code":"ASR_RATE_LIMITED"}', encoding="utf-8")
    with pytest.raises(AnalysisFailure, match="ASR_RATE_LIMITED"):
        _read_asr_result(response, 0, 256)


def test_asr_adapter_enforces_total_deadline_for_a_slow_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "segment.wav"
    audio.write_bytes(b"wave-data")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "private-test-key")

    def slow_response(_: httpx.Request) -> httpx.Response:
        time.sleep(0.1)
        return httpx.Response(200, json={"text": "late response"})

    with pytest.raises(AsrCallError, match="ASR_TIMEOUT"):
        transcribe(
            audio,
            base_url="https://api.siliconflow.cn/v1",
            model="FunAudioLLM/SenseVoiceSmall",
            total_timeout=0.05,
            connect_timeout=1,
            read_timeout=1,
            maximum_response_bytes=256,
            parameters={},
            transport=httpx.MockTransport(slow_response),
        )


def test_celery_delivery_and_acknowledgement_settings_are_explicit() -> None:
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_acks_late is False
    assert celery_app.conf.task_reject_on_worker_lost is False
    assert celery_app.conf.task_publish_retry is False
    assert celery_app.conf.worker_concurrency == 1
    assert celery_app.conf.worker_prefetch_multiplier == 1
