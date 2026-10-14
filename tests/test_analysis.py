from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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

import videoclip.analysis_delivery as analysis_delivery
import videoclip.analysis_worker as analysis_worker
import videoclip.stop_confirmation as stop_confirmation
from videoclip.analysis import (
    build_configuration_snapshot,
    canonical_sha256,
    recover_analysis_attempt,
    submit_analysis_task,
    task_response,
)
from videoclip.analysis_delivery import (
    DeliveryPolicy,
    coordinate_analysis_delivery,
    coordinate_due_analysis_deliveries,
)
from videoclip.analysis_lifecycle import coordinate_due_analysis_recoveries, create_followup_attempt
from videoclip.analysis_supervisor import BoundaryUnknown, WorkerBoundary
from videoclip.analysis_worker import (
    ASR_REQUEST_FINGERPRINT_VERSION,
    AnalysisFailure,
    ExecutionLease,
    _claim_attempt,
    _deletion_cleanup_is_pending,
    _persist_transcript,
    _read_asr_result,
    _run_process,
    _save_manifest,
    _set_stage,
    _start_segment_request,
    build_manifest,
    transcribe_analysis,
)
from videoclip.asr_client import AsrCallError, transcribe
from videoclip.celery_app import celery_app
from videoclip.execution_gate import EXECUTION_ADVISORY_LOCK_KEY
from videoclip.main import create_app
from videoclip.models import (
    AnalysisAttempt,
    AnalysisEvent,
    AnalysisExecution,
    AnalysisSummary,
    AnalysisTask,
    SharedModelCooldown,
    SummaryRequest,
    TaskDelivery,
    TranscriptSegment,
    UploadHandoff,
    Video,
)
from videoclip.recovery_rules import checkpoint_matches, evaluate_recovery
from videoclip.schemas import AnalysisTaskResponse
from videoclip.services import delete_video, delete_video_media
from videoclip.settings import Settings
from videoclip.stop_confirmation import (
    PROCESS_SCOPE,
    confirm_operator_container_stop,
    confirm_stopped_execution,
    worker_stop_evidence,
)
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


def _mark_attempt_failed_stopped(
    factory: sessionmaker[Session],
    task_id: uuid.UUID,
    attempt_id: uuid.UUID,
    *,
    failure_code: str = "ASR_REQUEST_FAILED",
) -> None:
    now = datetime.now(UTC)
    with factory.begin() as session:
        task = session.get(AnalysisTask, task_id)
        attempt = session.get(AnalysisAttempt, attempt_id)
        assert task is not None and attempt is not None
        execution = AnalysisExecution(
            task_id=task.id,
            attempt_id=attempt.id,
            worker_id="test-confirmed-worker",
            backend_pid=1,
            worker_instance_id=uuid.uuid4(),
            worker_container_id="a" * 64,
            process_scope=PROCESS_SCOPE,
            boundary_pid=111,
            boundary_start_ticks=222,
            boundary_session_id=111,
            boundary_boot_id="test-boot-id",
            supervisor_registered_at=now - timedelta(seconds=1),
        )
        session.add(execution)
        session.flush()
        attempt.claim_count = 1
        attempt.execution_id = execution.id
        attempt.first_claimed_at = now - timedelta(seconds=1)
        attempt.status = "failed"
        attempt.failure_code = failure_code
        attempt.finished_at = now
        task.status = "failed"
        task.stage = "failed"
        task.failure_code = failure_code
        if task.delivery_deadline_at is not None:
            task.delivery_blocked_since = attempt.first_claimed_at
    with factory() as session:
        execution = session.scalar(
            select(AnalysisExecution).where(AnalysisExecution.attempt_id == attempt_id)
        )
        identity = stop_confirmation.identity_from_execution(execution) if execution is not None else None
        session.commit()
    assert identity is not None
    evidence = worker_stop_evidence(identity, session_stopped=True)
    assert confirm_stopped_execution(factory, evidence) == "confirmed"


def test_submit_freezes_configuration_and_is_idempotent(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    published: list[tuple[str, str]] = []
    app = create_app(
        test_settings,
        session_factory,
        MediaStore(test_settings.app_data_root),
        run_recovery=False,
        task_publisher=lambda task_id, attempt_id: published.append((task_id, attempt_id)),
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
        assert len(published) == 1 and published[0] == (str(first.id), str(first.attempt.id))

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
            detail = client.get(f"/videos/{video_id}")
            assert detail.status_code == 200
            assert detail.json()["analysis_task_exists"] is True
            assert detail.json()["analysis_task_id"] == str(first.id)
            assert task is not None and task.status == "queued"
            assert attempt is not None and attempt.claim_count == 0
            assert attempt.config_sha256 == canonical_sha256(attempt.config_snapshot)
            assert attempt.config_snapshot["source"]["fingerprint"] == attempt.source_fingerprint
            assert attempt.config_snapshot["asr"]["secret_reference"] == "environment:SILICONFLOW_API_KEY"
            assert "api_key" not in attempt.config_snapshot["asr"]

        removed = client.delete(f"/videos/{video_id}", headers=headers)
        assert removed.status_code == 204


def test_duplicate_submission_returns_the_existing_terminal_task(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    published: list[tuple[str, str]] = []
    app = create_app(
        test_settings,
        session_factory,
        MediaStore(test_settings.app_data_root),
        run_recovery=False,
        task_publisher=lambda task_id, attempt_id: published.append((task_id, attempt_id)),
    )
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-terminal-idempotency")
        video_id = _video(session_factory, app.state.store, owner_id)
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        first = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert first.status_code == 202
        task_id = uuid.UUID(first.json()["id"])
        attempt_id = uuid.UUID(first.json()["current_attempt_id"])
        with session_factory.begin() as session:
            task = session.get(AnalysisTask, task_id)
            attempt = session.get(AnalysisAttempt, attempt_id)
            assert task is not None and attempt is not None
            task.status = "failed"
            task.stage = "failed"
            task.failure_code = "TEST_TERMINAL_FIXTURE"
            attempt.status = "failed"
            attempt.failure_code = "TEST_TERMINAL_FIXTURE"

        repeated = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert repeated.status_code == 202
        assert repeated.json()["id"] == str(task_id)
        assert repeated.json()["status"] == "failed"
        assert len(published) == 1


def test_submission_and_task_reads_are_owner_scoped(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    app = create_app(test_settings, session_factory, run_recovery=False, task_publisher=lambda *_: None)
    with TestClient(app) as owner, TestClient(app) as stranger:
        owner_id = _register(owner, "analysis-owner-two")
        stranger_id = _register(stranger, "analysis-stranger")
        video_id = _video(session_factory, app.state.store, owner_id)
        from tests.helpers import csrf, mutate_headers

        created = owner.post(f"/videos/{video_id}/tasks", headers=mutate_headers(csrf(owner)))
        assert created.status_code == 202
        task_id = created.json()["id"]
        assert stranger_id != owner_id
        assert owner.get(f"/videos/{video_id}").json()["analysis_task_id"] == task_id
        assert stranger.get(f"/videos/{video_id}").status_code == 404
        assert stranger.get(f"/videos/{video_id}/tasks/{task_id}").status_code == 404
        assert stranger.get(f"/videos/{video_id}/transcript").status_code == 404
        stranger_headers = mutate_headers(csrf(stranger))
        assert stranger.post(f"/videos/{video_id}/tasks", headers=stranger_headers).status_code == 404


def test_broker_error_is_unknown_and_does_not_republish_on_duplicate(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    calls = 0

    def broker_failure(_: str, __: str) -> None:
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

    def serialization_failure(_: str, __: str) -> None:
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
    session_factory: sessionmaker[Session],
    test_settings: Settings,
) -> None:
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-pending-delivery")
        video_id = _video(session_factory, MediaStore(test_settings.app_data_root), owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    started_at = datetime.now(UTC)

    def exit_after_operation_reservation(_task_id: str, _attempt_id: str) -> None:
        raise SystemExit("injected process exit after durable operation reservation")

    with pytest.raises(SystemExit, match="durable operation reservation"):
        coordinate_analysis_delivery(
            session_factory,
            submitted.task_id,
            submitted.attempt_id,
            exit_after_operation_reservation,
            now=started_at,
        )
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        delivery = session.get(TaskDelivery, submitted.attempt_id)
        assert task is not None and task.delivery_retry_count == 0
        assert delivery is not None and delivery.status == "pending"
        assert delivery.operation_generation == 1

    published: list[tuple[str, str]] = []
    retry_at = started_at + timedelta(seconds=61)
    result = coordinate_analysis_delivery(
        session_factory,
        submitted.task_id,
        submitted.attempt_id,
        lambda task_id, attempt_id: published.append((task_id, attempt_id)),
        now=retry_at,
    )
    assert result == "sent"
    assert published == [(str(submitted.task_id), str(submitted.attempt_id))]
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        delivery = session.get(TaskDelivery, submitted.attempt_id)
        assert task is not None and task.delivery_retry_count == 1
        assert delivery is not None and delivery.status == "sent"
        assert delivery.operation_generation == 2


def test_successful_publish_with_lost_confirmation_stays_pending(
    test_settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, str]] = []

    def lose_sent_confirmation(*_args: Any, **_kwargs: Any) -> bool:
        raise SQLAlchemyError("injected lost sent confirmation")

    monkeypatch.setattr(analysis_delivery, "_record_delivery_outcome", lose_sent_confirmation)
    app = create_app(
        test_settings,
        session_factory,
        run_recovery=False,
        task_publisher=lambda task_id, attempt_id: published.append((task_id, attempt_id)),
    )
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


def test_http_submission_and_due_scanner_race_is_generation_safe(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    entered = threading.Event()
    release = threading.Event()
    published: list[tuple[str, str]] = []

    def publisher(task_id: str, attempt_id: str) -> None:
        published.append((task_id, attempt_id))
        if len(published) == 1:
            entered.set()
            assert release.wait(10)
            raise OSError("injected uncertain first send")

    store = MediaStore(test_settings.app_data_root)
    app = create_app(test_settings, session_factory, store, run_recovery=False, task_publisher=publisher)
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-http-scanner-race")
        video_id = _video(session_factory, store, owner_id)
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        with ThreadPoolExecutor(max_workers=1) as pool:
            submission = pool.submit(client.post, f"/videos/{video_id}/tasks", headers=headers)
            assert entered.wait(10)
            with session_factory() as session:
                task = session.scalar(select(AnalysisTask).where(AnalysisTask.video_id == video_id))
                assert task is not None and task.current_attempt_id is not None
                task_id, attempt_id = task.id, task.current_attempt_id

            scan = coordinate_due_analysis_deliveries(
                session_factory,
                publisher,
                now=datetime.now(UTC) + timedelta(seconds=61),
            )
            assert scan.scanned == 1 and scan.published == 1
            lease = ExecutionLease.acquire(test_engine)
            assert lease is not None
            try:
                claim = _claim_attempt(lease, task_id, attempt_id, "test-http-scanner-race")
                assert claim is not None
            finally:
                lease.release()
            release.set()
            response = submission.result(timeout=15)

        assert response.status_code == 202
        assert response.json()["status"] == "running"
        assert len(published) == 2
        with session_factory() as session:
            task = session.get(AnalysisTask, task_id)
            delivery = session.get(TaskDelivery, attempt_id)
            attempt = session.get(AnalysisAttempt, attempt_id)
            assert task is not None and task.status == "running"
            assert task.delivery_operation_count == 2 and task.delivery_retry_count == 1
            assert delivery is not None and delivery.operation_generation == 2
            assert delivery.status == "sent"
            assert attempt is not None and attempt.claim_count == 1


def test_deletion_invalidates_an_inflight_delivery_and_its_late_message(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-delete-delivery-race")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    entered = threading.Event()
    release = threading.Event()

    def blocked_publisher(_task_id: str, _attempt_id: str) -> None:
        entered.set()
        assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        publication = pool.submit(
            coordinate_analysis_delivery,
            session_factory,
            submitted.task_id,
            submitted.attempt_id,
            blocked_publisher,
        )
        assert entered.wait(10)
        assert delete_video(session_factory, store, owner_id, video_id) == 204
        release.set()
        assert publication.result(timeout=15) == "superseded"

    lease = ExecutionLease.acquire(test_engine)
    assert lease is not None
    try:
        assert (
            _claim_attempt(lease, submitted.task_id, submitted.attempt_id, "test-deleted-late-message")
            is None
        )
    finally:
        lease.release()
    with session_factory() as session:
        assert session.get(Video, video_id) is None
        assert session.get(AnalysisTask, submitted.task_id) is None
        assert session.get(TaskDelivery, submitted.attempt_id) is None
        assert session.scalar(select(func.count(AnalysisExecution.id))) == 0


def test_broker_error_cannot_overwrite_claimed_execution(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)

    def claim_then_fail(task_id_value: str, attempt_id_value: str) -> None:
        lease = ExecutionLease.acquire(test_engine)
        assert lease is not None
        assert (
            _claim_attempt(
                lease, uuid.UUID(task_id_value), uuid.UUID(attempt_id_value), "publisher-race-worker"
            )
            is not None
        )
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
        assert response.json()["delivery_status"] == "pending"
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
    claim = _claim_attempt(lease, submitted.task_id, submitted.attempt_id, "test-worker-lock-loss")
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
    assert (
        _claim_attempt(next_lease, submitted.task_id, submitted.attempt_id, "test-worker-after-lock-loss")
        is None
    )
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
    claim = _claim_attempt(lease, submitted.task_id, submitted.attempt_id, "test-worker-invalidated-video")
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
    first_claim = _claim_attempt(first_lease, submitted.task_id, submitted.attempt_id, "test-worker-one")
    assert first_claim is not None
    first_lease.release()

    second_lease = ExecutionLease.acquire(test_engine)
    assert second_lease is not None
    duplicate_claim = _claim_attempt(second_lease, submitted.task_id, submitted.attempt_id, "test-worker-two")
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
        video_c = _video(session_factory, store, owner_id)
    submitted_a = submit_analysis_task(session_factory, owner_id, video_a, test_settings)
    first_lease = ExecutionLease.acquire(test_engine)
    assert first_lease is not None
    claim_a = _claim_attempt(first_lease, submitted_a.task_id, submitted_a.attempt_id, "test-worker-one")
    assert claim_a is not None
    first_lease.release()

    # Other videos may obtain durable queue eligibility while the first execution owns the slot.
    submitted_b = submit_analysis_task(session_factory, owner_id, video_b, test_settings)
    submitted_c = submit_analysis_task(session_factory, owner_id, video_c, test_settings)
    published: list[tuple[str, str]] = []
    now = datetime.now(UTC) + timedelta(seconds=1)
    for submitted in (submitted_b, submitted_c):
        assert (
            coordinate_analysis_delivery(
                session_factory,
                submitted.task_id,
                submitted.attempt_id,
                lambda task_id, attempt_id: published.append((task_id, attempt_id)),
                now=now,
            )
            == "blocked"
        )
    assert published == []

    second_lease = ExecutionLease.acquire(test_engine)
    assert second_lease is not None
    assert (
        _claim_attempt(second_lease, submitted_b.task_id, submitted_b.attempt_id, "test-worker-two") is None
    )
    second_lease.release()
    # Early ACK may consume B's message at the real analysis task entry point while A owns the slot.
    assert transcribe_analysis(str(submitted_b.task_id), str(submitted_b.attempt_id)) == {
        "status": "ignored_not_eligible"
    }
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted_b.task_id)
        assert task is not None and task.status == "queued"
        assert task.delivery_wait_reason == "stop_confirmation_required"
        assert task.delivery_retry_count == 0
        assert task.delivery_deadline_at is None
        queued_c = session.get(AnalysisTask, submitted_c.task_id)
        assert queued_c is not None and queued_c.status == "queued"
        assert queued_c.delivery_retry_count == 0
        assert (
            session.scalar(
                select(func.count(AnalysisExecution.id)).where(AnalysisExecution.stop_confirmed_at.is_(None))
            )
            == 1
        )

    release_lease = ExecutionLease.acquire(test_engine)
    assert release_lease is not None
    release_maker = sessionmaker(bind=release_lease.connection, autoflush=False, expire_on_commit=False)
    analysis_worker._confirm_inline_test_stop(
        release_maker,
        release_lease,
        claim_a,
        cleanup_state="cleaned",
        reason="controlled_execution_fixture",
    )
    release_lease.release()
    status_b = task_response(session_factory, owner_id, video_b, submitted_b.task_id)
    status_c = task_response(session_factory, owner_id, video_c, submitted_c.task_id)
    assert status_b.delivery_wait_reason == "queued_for_delivery"
    assert status_c.delivery_wait_reason == "queue_order_wait"
    resumed: list[str] = []
    resumed_at = now + timedelta(seconds=61)
    cycle = coordinate_due_analysis_deliveries(
        session_factory,
        lambda task_id, _attempt_id: resumed.append(task_id),
        now=resumed_at,
    )
    assert cycle.scanned == 2 and cycle.published == 1
    with session_factory() as session:
        queued = [session.get(AnalysisTask, item) for item in (submitted_b.task_id, submitted_c.task_id)]
        tasks = [item for item in queued if item is not None]
        assert len(tasks) == 2 and all(item.queue_entered_at is not None for item in tasks)
        expected = [str(item.id) for item in sorted(tasks, key=lambda task: (task.queue_entered_at, task.id))]
    assert resumed == expected[:1]

    second_lease = ExecutionLease.acquire(test_engine)
    assert second_lease is not None
    # A later message arriving first still cannot steal the persisted queue position.
    assert (
        _claim_attempt(second_lease, submitted_c.task_id, submitted_c.attempt_id, "test-worker-out-of-order")
        is None
    )
    second_lease.release()
    assert transcribe_analysis(str(submitted_c.task_id), str(submitted_c.attempt_id)) == {
        "status": "ignored_not_eligible"
    }
    second_lease = ExecutionLease.acquire(test_engine)
    assert second_lease is not None
    claim_b = _claim_attempt(second_lease, submitted_b.task_id, submitted_b.attempt_id, "test-worker-two")
    assert claim_b is not None
    second_lease.release()
    assert transcribe_analysis(str(submitted_c.task_id), str(submitted_c.attempt_id)) == {
        "status": "ignored_not_eligible"
    }
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count(AnalysisExecution.id)).where(AnalysisExecution.stop_confirmed_at.is_(None))
            )
            == 1
        )

    release_lease = ExecutionLease.acquire(test_engine)
    assert release_lease is not None
    release_maker = sessionmaker(bind=release_lease.connection, autoflush=False, expire_on_commit=False)
    analysis_worker._confirm_inline_test_stop(
        release_maker,
        release_lease,
        claim_b,
        cleanup_state="cleaned",
        reason="controlled_execution_fixture",
    )
    release_lease.release()
    coordinate_due_analysis_deliveries(
        session_factory,
        lambda task_id, _attempt_id: resumed.append(task_id),
        now=resumed_at + timedelta(seconds=61),
    )
    third_lease = ExecutionLease.acquire(test_engine)
    assert third_lease is not None
    claim_c = _claim_attempt(third_lease, submitted_c.task_id, submitted_c.attempt_id, "test-worker-three")
    assert claim_c is not None
    third_lease.release()


def test_delivery_reservation_contends_on_the_worker_execution_gate(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-delivery-execution-gate")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    now = datetime.now(UTC) + timedelta(seconds=1)
    policy = DeliveryPolicy(interval_seconds=10, operation_timeout_seconds=10)
    published: list[tuple[str, str]] = []

    locker = test_engine.connect()
    try:
        assert locker.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": EXECUTION_ADVISORY_LOCK_KEY},
        ).scalar_one()
        locker.commit()
        assert (
            coordinate_analysis_delivery(
                session_factory,
                submitted.task_id,
                submitted.attempt_id,
                lambda task_id, attempt_id: published.append((task_id, attempt_id)),
                now=now,
                policy=policy,
            )
            == "blocked"
        )
        with session_factory() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            delivery = session.get(TaskDelivery, submitted.attempt_id)
            assert task is not None and task.delivery_wait_reason == "execution_slot_occupied"
            assert task.delivery_retry_count == 0 and task.delivery_deadline_at is None
            assert delivery is not None and delivery.operation_generation == 0
        assert published == []
    finally:
        locker.execute(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": EXECUTION_ADVISORY_LOCK_KEY},
        )
        locker.commit()
        locker.close()

    assert (
        coordinate_analysis_delivery(
            session_factory,
            submitted.task_id,
            submitted.attempt_id,
            lambda task_id, attempt_id: published.append((task_id, attempt_id)),
            now=now + timedelta(seconds=11),
            policy=policy,
        )
        == "sent"
    )
    assert published == [(str(submitted.task_id), str(submitted.attempt_id))]
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        delivery = session.get(TaskDelivery, submitted.attempt_id)
        assert task is not None and task.delivery_retry_count == 0 and task.delivery_operation_count == 1
        assert delivery is not None and delivery.operation_generation == 1


def test_delivery_scan_uses_stable_queue_order_not_message_order(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-delivery-order")
        video_ids = [_video(session_factory, store, owner_id) for _ in range(3)]
    submitted = [
        submit_analysis_task(session_factory, owner_id, video_id, test_settings) for video_id in video_ids
    ]
    queue_time = datetime.now(UTC) + timedelta(seconds=1)
    with session_factory.begin() as session:
        for item in submitted:
            task = session.get(AnalysisTask, item.task_id)
            assert task is not None
            task.queue_entered_at = queue_time
            task.delivery_next_check_at = queue_time
    expected_order = [str(item.task_id) for item in sorted(submitted, key=lambda item: item.task_id.int)]
    later_submission = next(item for item in submitted if str(item.task_id) == expected_order[-1])
    published: list[str] = []
    published_out_of_order: list[str] = []
    assert (
        coordinate_analysis_delivery(
            session_factory,
            later_submission.task_id,
            later_submission.attempt_id,
            lambda task_id, _attempt_id: published_out_of_order.append(task_id),
            now=queue_time,
        )
        == "queue_order_wait"
    )
    assert published_out_of_order == []

    result = coordinate_due_analysis_deliveries(
        session_factory,
        lambda task_id, _attempt_id: published.append(task_id),
        now=queue_time + timedelta(seconds=61),
        policy=DeliveryPolicy(scan_limit=3),
    )
    assert result.scanned == 3
    assert result.published == 1
    assert published == expected_order[:1]
    with session_factory() as session:
        later_tasks = [session.get(AnalysisTask, item.task_id) for item in submitted]
        assert all(
            task is not None
            and (
                task.delivery_wait_reason == "delivery_claim_pending"
                if str(task.id) == expected_order[0]
                else task.delivery_wait_reason == "queue_order_wait"
            )
            for task in later_tasks
        )


def test_delivery_retries_are_idempotent_bounded_and_independent_of_execution_budget(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-delivery-budget")
        video_id = _video(session_factory, store, owner_id)
    delivery_settings = test_settings.model_copy(update={"analysis_delivery_retry_limit": 2})
    submitted = submit_analysis_task(session_factory, owner_id, video_id, delivery_settings)
    base = datetime.now(UTC) + timedelta(seconds=1)
    policy = DeliveryPolicy(
        retry_limit=2,
        deadline_seconds=1000,
        scan_limit=4,
        interval_seconds=10,
        operation_timeout_seconds=10,
        backoff_base_seconds=10,
        backoff_max_seconds=40,
    )
    publishes: list[tuple[str, str]] = []

    def broker_unknown(task_id: str, attempt_id: str) -> None:
        publishes.append((task_id, attempt_id))
        raise OSError("injected unknown broker result")

    first = coordinate_due_analysis_deliveries(session_factory, broker_unknown, now=base, policy=policy)
    assert first.scanned == 1 and first.published == 1
    assert (
        coordinate_analysis_delivery(
            session_factory,
            submitted.task_id,
            submitted.attempt_id,
            broker_unknown,
            now=base,
            policy=policy,
        )
        == "not_due"
    )
    assert (
        coordinate_due_analysis_deliveries(
            session_factory, broker_unknown, now=base + timedelta(seconds=9), policy=policy
        ).scanned
        == 0
    )
    coordinate_due_analysis_deliveries(
        session_factory, broker_unknown, now=base + timedelta(seconds=10), policy=policy
    )
    coordinate_due_analysis_deliveries(
        session_factory, broker_unknown, now=base + timedelta(seconds=30), policy=policy
    )
    exhausted = coordinate_due_analysis_deliveries(
        session_factory, broker_unknown, now=base + timedelta(seconds=70), policy=policy
    )
    assert exhausted.exhausted == 1
    assert len(publishes) == 3

    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        attempt = session.get(AnalysisAttempt, submitted.attempt_id)
        delivery = session.get(TaskDelivery, submitted.attempt_id)
        assert task is not None and task.status == "failed"
        assert task.failure_code == "ANALYSIS_DELIVERY_RETRIES_EXHAUSTED"
        assert task.delivery_failure_code == "ANALYSIS_DELIVERY_RETRIES_EXHAUSTED"
        assert task.delivery_retry_count == 2 and task.delivery_operation_count == 3
        assert attempt is not None and attempt.status == "failed" and attempt.claim_count == 0
        assert delivery is not None and delivery.status == "failed"
        assert delivery.operation_generation == 3
        assert task.current_attempt_id == submitted.attempt_id
        assert session.scalar(select(func.count(SummaryRequest.id))) == 0

    assert (
        coordinate_due_analysis_deliveries(
            session_factory, broker_unknown, now=base + timedelta(seconds=200), policy=policy
        ).scanned
        == 0
    )
    assert len(publishes) == 3


def test_delivery_deadline_pauses_for_execution_slot_occupancy(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-delivery-deadline")
        blocker_video = _video(session_factory, store, owner_id)
        video_id = _video(session_factory, store, owner_id)
    blocker = submit_analysis_task(session_factory, owner_id, blocker_video, test_settings)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    policy = DeliveryPolicy(
        retry_limit=3,
        deadline_seconds=15,
        scan_limit=4,
        interval_seconds=10,
        operation_timeout_seconds=10,
        backoff_base_seconds=10,
        backoff_max_seconds=60,
    )
    base = datetime.now(UTC) + timedelta(seconds=1)
    blocked_lease = ExecutionLease.acquire(test_engine)
    assert blocked_lease is not None
    try:
        blocker_claim = _claim_attempt(blocked_lease, blocker.task_id, blocker.attempt_id, "deadline-blocker")
    finally:
        blocked_lease.release()
    assert blocker_claim is not None
    with session_factory.begin() as session:
        execution = session.get(AnalysisExecution, blocker_claim.execution_id)
        assert execution is not None
        execution.started_at = base
    assert (
        coordinate_analysis_delivery(
            session_factory, submitted.task_id, submitted.attempt_id, lambda *_: None, now=base, policy=policy
        )
        == "blocked"
    )
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None and task.delivery_deadline_at is None
        assert task.delivery_retry_count == 0

    # A scheduler outage must not count time already covered by the blocking execution.
    with session_factory.begin() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None
        task.delivery_deadline_at = base + timedelta(seconds=5)
    later = base + timedelta(days=2)
    assert (
        coordinate_analysis_delivery(
            session_factory,
            submitted.task_id,
            submitted.attempt_id,
            lambda *_: None,
            now=later,
            policy=policy,
        )
        == "blocked"
    )
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None and task.status == "queued"
        assert task.delivery_retry_count == 0
        assert task.delivery_failure_code is None
        assert task.delivery_blocked_since == base

    release_lease = ExecutionLease.acquire(test_engine)
    assert release_lease is not None
    release_maker = sessionmaker(bind=release_lease.connection, autoflush=False, expire_on_commit=False)
    analysis_worker._confirm_inline_test_stop(
        release_maker,
        release_lease,
        blocker_claim,
        cleanup_state="cleaned",
        reason="controlled_execution_fixture",
    )
    release_lease.release()
    assert (
        coordinate_analysis_delivery(
            session_factory,
            submitted.task_id,
            submitted.attempt_id,
            lambda *_: None,
            now=later + timedelta(seconds=11),
            policy=policy,
        )
        == "sent"
    )
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None and task.status == "queued"
        assert task.delivery_retry_count == 0
        assert task.delivery_deadline_at is not None
        assert task.delivery_deadline_at > later + timedelta(seconds=11)


def test_delivery_deadline_pauses_for_recorded_execution_block_and_expires_separately(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-delivery-deadline-pause")
        video_id = _video(session_factory, store, owner_id)
    delivery_settings = test_settings.model_copy(update={"analysis_delivery_retry_limit": 5})
    submitted = submit_analysis_task(session_factory, owner_id, video_id, delivery_settings)
    base = datetime.now(UTC) + timedelta(seconds=1)
    policy = DeliveryPolicy(
        retry_limit=5,
        deadline_seconds=15,
        scan_limit=4,
        interval_seconds=10,
        operation_timeout_seconds=10,
        backoff_base_seconds=10,
        backoff_max_seconds=60,
    )

    def unknown(_task_id: str, _attempt_id: str) -> None:
        raise OSError("injected broker uncertainty")

    assert (
        coordinate_analysis_delivery(
            session_factory, submitted.task_id, submitted.attempt_id, unknown, now=base, policy=policy
        )
        == "unknown"
    )
    with session_factory.begin() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None
        task.delivery_blocked_since = base + timedelta(seconds=5)
        task.delivery_next_check_at = base + timedelta(seconds=20)

    # The recorded pause shifts the fixed deadline forward before a due retry is accepted.
    assert (
        coordinate_analysis_delivery(
            session_factory,
            submitted.task_id,
            submitted.attempt_id,
            unknown,
            now=base + timedelta(seconds=20),
            policy=policy,
        )
        == "unknown"
    )
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None
        assert task.delivery_deadline_at == base + timedelta(seconds=30)
        assert task.delivery_retry_count == 1

    terminal = coordinate_due_analysis_deliveries(
        session_factory,
        unknown,
        now=base + timedelta(seconds=40),
        policy=policy,
    )
    assert terminal.exhausted == 1
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None and task.failure_code == "ANALYSIS_DELIVERY_DEADLINE_EXPIRED"
        assert task.delivery_retry_count == 1 and task.delivery_retry_limit == 5


def test_stale_delivery_result_cannot_overwrite_a_newer_generation(
    test_settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-delivery-generation")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    base = datetime.now(UTC) + timedelta(seconds=1)
    policy = DeliveryPolicy(operation_timeout_seconds=10, backoff_base_seconds=10)
    entered = threading.Event()
    release = threading.Event()

    def late_unknown(_task_id: str, _attempt_id: str) -> None:
        entered.set()
        assert release.wait(10)
        raise OSError("late result from generation one")

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            coordinate_analysis_delivery,
            session_factory,
            submitted.task_id,
            submitted.attempt_id,
            late_unknown,
            now=base,
            policy=policy,
        )
        assert entered.wait(10)
        second = coordinate_analysis_delivery(
            session_factory,
            submitted.task_id,
            submitted.attempt_id,
            lambda *_: None,
            now=base + timedelta(seconds=11),
            policy=policy,
        )
        assert second == "sent"
        release.set()
        assert first.result(timeout=10) == "superseded"

    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        delivery = session.get(TaskDelivery, submitted.attempt_id)
        assert task is not None and task.delivery_retry_count == 1
        assert delivery is not None and delivery.operation_generation == 2
        assert delivery.status == "sent" and delivery.error_code is None


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
    claim = _claim_attempt(lease, submitted.task_id, submitted.attempt_id, "test-worker-checkpoint")
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
    assert _claim_attempt(next_lease, submitted.task_id, submitted.attempt_id, "test-worker-replay") is None
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
    claim = _claim_attempt(lease, submitted.task_id, submitted.attempt_id, "test-worker-stale")
    assert claim is not None
    stale = claim.__class__(
        task_id=claim.task_id,
        attempt_id=claim.attempt_id,
        parent_attempt_id=claim.parent_attempt_id,
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
    monkeypatch.setattr(worker, "db_transaction", lambda _maker: nullcontext(None))
    monkeypatch.setattr(worker, "_lock_execution_rows", lambda *_args: None)
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
            assert exc.status_code in {404, 409}
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
    assert "deleted" in results
    assert "submitted" in results or "submit_rejected" in results


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


def test_recovery_rules_separate_delivery_age_from_stop_confirmation() -> None:
    now = datetime.now(UTC)
    common = {
        "video_status": "ready",
        "task_status": "queued",
        "attempt_status": "queued",
        "claim_count": 0,
        "delivery_updated_at": now,
        "attempt_stop_confirmed": False,
        "execution_stop_confirmed": False,
        "is_current_attempt": True,
        "now": now,
    }
    for delivery_status in ("pending", "unknown", "failed", "sent"):
        queued = evaluate_recovery(delivery_status=delivery_status, **common)
        assert not queued.eligible and queued.error_code == "DELIVERY_COORDINATION_ACTIVE"

    claimed = evaluate_recovery(
        **{
            **common,
            "task_status": "failed",
            "attempt_status": "failed",
            "delivery_status": "sent",
            "claim_count": 1,
        }
    )
    assert not claimed.eligible and claimed.error_code == "STOP_CONFIRMATION_REQUIRED"
    stopped = evaluate_recovery(
        **{
            **common,
            "task_status": "failed",
            "attempt_status": "failed",
            "delivery_status": "sent",
            "claim_count": 1,
            "attempt_stop_confirmed": True,
            "execution_stop_confirmed": True,
        }
    )
    assert stopped.eligible
    complete = evaluate_recovery(
        **{
            **common,
            "task_status": "awaiting_summary",
            "attempt_status": "awaiting_summary",
            "delivery_status": "sent",
            "claim_count": 1,
            "attempt_stop_confirmed": True,
            "execution_stop_confirmed": True,
        }
    )
    assert not complete.eligible and complete.error_code == "TRANSCRIPT_COMPLETE_AWAITING_SUMMARY"
    deleting = evaluate_recovery(**{**common, "video_status": "deleting", "delivery_status": "pending"})
    assert not deleting.eligible and deleting.error_code == "VIDEO_DELETION_IN_PROGRESS"

    exhausted = evaluate_recovery(delivery_status="failed", delivery_budget_exhausted=True, **common)
    assert not exhausted.eligible and exhausted.error_code == "ANALYSIS_DELIVERY_BUDGET_EXHAUSTED"


def test_manual_recovery_is_idempotent_and_old_attempt_messages_are_rejected(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    published: list[tuple[str, str]] = []
    app = create_app(
        test_settings,
        session_factory,
        run_recovery=False,
        task_publisher=lambda task_id, attempt_id: published.append((task_id, attempt_id)),
    )
    with TestClient(app) as client, TestClient(app) as stranger:
        owner_id = _register(client, "analysis-manual-recovery")
        _register(stranger, "analysis-recovery-stranger")
        video_id = _video(session_factory, app.state.store, owner_id)
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        stranger_headers = mutate_headers(csrf(stranger))
        original = client.post(f"/videos/{video_id}/tasks", headers=headers)
        assert original.status_code == 202
        task_id = uuid.UUID(original.json()["id"])
        parent_id = uuid.UUID(original.json()["attempt"]["id"])
        deadline_before_recovery = datetime.now(UTC) + timedelta(hours=1)
        with session_factory.begin() as session:
            task = session.get(AnalysisTask, task_id)
            delivery = session.get(TaskDelivery, parent_id)
            assert task is not None and task.queue_entered_at is not None
            assert delivery is not None
            original_queue_time = task.queue_entered_at
            task.delivery_retry_count = 2
            task.delivery_retry_limit = 8
            task.delivery_operation_count = 4
            task.delivery_deadline_at = deadline_before_recovery
            delivery.operation_generation = 4
        _mark_attempt_failed_stopped(session_factory, task_id, parent_id)

        url = f"/videos/{video_id}/tasks/{task_id}/recover"
        request = {"attempt_id": str(parent_id)}
        missing_csrf = client.post(url, json=request)
        assert missing_csrf.status_code == 403
        hidden = stranger.post(url, json=request, headers=stranger_headers)
        assert hidden.status_code == 404
        recovered = client.post(url, json=request, headers=headers)
        assert recovered.status_code == 202, recovered.text
        child_id = uuid.UUID(recovered.json()["attempt"]["id"])
        assert recovered.json()["attempt"]["parent_attempt_id"] == str(parent_id)
        assert recovered.json()["attempt"]["attempt_number"] == 2
        assert recovered.json()["status"] == "queued"
        assert len(published) == 2
        assert published[1] == (str(task_id), str(child_id))

        replayed = client.post(url, json=request, headers=headers)
        assert replayed.status_code == 202
        assert replayed.json()["attempt"]["id"] == str(child_id)
        assert len(published) == 2
        with session_factory() as session:
            attempts = list(
                session.scalars(
                    select(AnalysisAttempt)
                    .where(AnalysisAttempt.task_id == task_id)
                    .order_by(AnalysisAttempt.attempt_number)
                )
            )
            assert len(attempts) == 2
            assert attempts[0].status == "failed"
            assert attempts[0].failure_code == "ASR_REQUEST_FAILED"
            assert attempts[1].parent_attempt_id == attempts[0].id
            assert attempts[1].config_sha256 == attempts[0].config_sha256
            assert attempts[1].work_directory_name != attempts[0].work_directory_name
            recovered_task = session.get(AnalysisTask, task_id)
            recovered_delivery = session.get(TaskDelivery, child_id)
            assert recovered_task is not None
            assert (
                recovered_task.queue_entered_at is not None
                and recovered_task.queue_entered_at > original_queue_time
            )
            assert recovered_task.delivery_retry_count == 3
            assert recovered_task.delivery_retry_limit == 8
            assert recovered_task.delivery_operation_count == 5
            assert recovered_task.delivery_deadline_at is not None
            assert recovered_task.delivery_deadline_at > deadline_before_recovery
            assert recovered_delivery is not None and recovered_delivery.operation_generation == 1

    lease = ExecutionLease.acquire(test_engine)
    assert lease is not None
    assert _claim_attempt(lease, task_id, parent_id, "test-old-message") is None
    assert _claim_attempt(lease, task_id, child_id, "test-current-message") is not None
    lease.release()
    assert transcribe_analysis(str(task_id)) == {"status": "ignored_legacy_task_message"}


def test_recovery_rejects_source_metadata_that_no_longer_matches_the_frozen_snapshot(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-source-changed")
        video_id = _video(session_factory, client.app.state.store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    _mark_attempt_failed_stopped(session_factory, submitted.task_id, submitted.attempt_id)
    with session_factory.begin() as session:
        video = session.get(Video, video_id)
        assert video is not None
        video.content_sha256 = "f" * 64
    with pytest.raises(HTTPException) as error:
        recover_analysis_attempt(session_factory, owner_id, video_id, submitted.task_id, submitted.attempt_id)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "SOURCE_CONFIGURATION_INCOMPATIBLE"
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        attempt = session.get(AnalysisAttempt, submitted.attempt_id)
        assert task is not None and task.current_attempt_id == submitted.attempt_id
        assert attempt is not None and attempt.status == "failed"


def test_recovery_and_video_deletion_serialize_on_the_video_row(
    test_settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-recovery-delete-race")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    start = threading.Barrier(2)

    def recover() -> str:
        start.wait()
        try:
            recover_analysis_attempt(
                session_factory,
                owner_id,
                video_id,
                submitted.task_id,
                submitted.attempt_id,
            )
            return "recovered"
        except HTTPException as exc:
            assert exc.status_code in {404, 409}
            return "recovery_rejected"

    def delete() -> str:
        start.wait()
        return "deleted" if delete_video(session_factory, store, owner_id, video_id) == 204 else "pending"

    with ThreadPoolExecutor(max_workers=2) as pool:
        recover_future = pool.submit(recover)
        delete_future = pool.submit(delete)
        result_recover = recover_future.result(timeout=20)
        result_delete = delete_future.result(timeout=20)

    assert result_recover in {"recovered", "recovery_rejected"}
    assert result_delete == "deleted"
    with session_factory() as session:
        assert session.get(Video, video_id) is None
        assert session.get(AnalysisTask, submitted.task_id) is None
        assert (
            session.scalar(
                select(func.count(AnalysisAttempt.id)).where(AnalysisAttempt.task_id == submitted.task_id)
            )
            == 0
        )


def test_stop_unconfirmed_blocks_recovery_and_deletion_until_confirmed(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    app = create_app(
        test_settings, session_factory, store, run_recovery=False, task_publisher=lambda *_: None
    )
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-delete-active")
        video_id = _video(session_factory, store, owner_id)
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        created = client.post(f"/videos/{video_id}/tasks", headers=headers)
        task_id = uuid.UUID(created.json()["id"])
        attempt_id = uuid.UUID(created.json()["attempt"]["id"])
        with session_factory() as session:
            video = session.get(Video, video_id)
            attempt = session.get(AnalysisAttempt, attempt_id)
            assert video is not None and attempt is not None
            target = store.target_path(video.storage_name)
            work = store.analysis / task_id.hex / attempt.work_directory_name
            work.mkdir(parents=True)
            sentinel = work / "audio.s16le"
            sentinel.write_bytes(b"active work")

        lease = ExecutionLease.acquire(test_engine)
        assert lease is not None
        active_claim = _claim_attempt(lease, task_id, attempt_id, "test-active-worker")
        assert active_claim is not None
        recovery = client.post(
            f"/videos/{video_id}/tasks/{task_id}/recover",
            json={"attempt_id": str(attempt_id)},
            headers=headers,
        )
        assert recovery.status_code == 409
        assert recovery.json()["detail"]["code"] == "STOP_CONFIRMATION_REQUIRED"

        pending_delete = client.delete(f"/videos/{video_id}", headers=headers)
        assert pending_delete.status_code == 202
        detail = client.get(f"/videos/{video_id}")
        assert detail.status_code == 409
        assert detail.json()["detail"]["code"] == "VIDEO_DELETION_IN_PROGRESS"
        lease_maker = sessionmaker(bind=lease.connection, autoflush=False, expire_on_commit=False)
        assert _deletion_cleanup_is_pending(lease_maker, active_claim) is True
        assert target.exists() and sentinel.exists()
        assert client.get(f"/videos/{video_id}/transcript").status_code == 409
        assert client.get(f"/videos/{video_id}/content").status_code == 404
        assert client.get(f"/videos/{video_id}/tasks/{task_id}").status_code == 409
        progress = client.get(f"/videos/{video_id}/deletion")
        assert progress.status_code == 200
        assert progress.json()["stop_confirmation_required"] is True

        lease.release()
        assert delete_video(session_factory, store, owner_id, video_id) == 202
        assert target.exists() and sentinel.exists()

        stopped_at = datetime.now(UTC)
        with session_factory.begin() as session:
            task = session.get(AnalysisTask, task_id)
            attempt = session.get(AnalysisAttempt, attempt_id)
            execution = session.scalar(
                select(AnalysisExecution).where(AnalysisExecution.attempt_id == attempt_id)
            )
            assert task is not None and attempt is not None and execution is not None
            task.status = "failed"
            attempt.status = "failed"
            attempt.stop_confirmed_at = stopped_at
            execution.stop_confirmed_at = stopped_at
            execution.stop_confirmation_source = "test_operator_evidence"

        original_remove = store.remove_attempt_directory

        def fail_temp_cleanup(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("injected temp cleanup failure")

        monkeypatch.setattr(store, "remove_attempt_directory", fail_temp_cleanup)
        failed_cleanup = client.delete(f"/videos/{video_id}", headers=headers)
        assert failed_cleanup.status_code == 202
        details = client.get(f"/videos/{video_id}/deletion")
        assert details.status_code == 200
        assert details.json()["state"] == "cleanup_failed"
        assert details.json()["error_code"] == "ATTEMPT_TEMP_CLEANUP_FAILED"
        assert target.exists() and sentinel.exists()

        monkeypatch.setattr(store, "remove_attempt_directory", original_remove)
        completed = client.delete(f"/videos/{video_id}", headers=headers)
        assert completed.status_code == 204
        assert not target.exists() and not sentinel.exists()
        with session_factory() as session:
            assert session.get(AnalysisTask, task_id) is None
            assert session.get(AnalysisAttempt, attempt_id) is None
            assert session.get(Video, video_id) is None


def test_stopped_attempt_cleanup_preserves_transcript_and_video(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    published: list[tuple[str, str]] = []
    app = create_app(
        test_settings,
        session_factory,
        store,
        run_recovery=False,
        task_publisher=lambda task_id, attempt_id: published.append((task_id, attempt_id)),
    )
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-cleanup-retry")
        video_id = _video(session_factory, store, owner_id)
        task = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
        lease = ExecutionLease.acquire(test_engine)
        assert lease is not None
        claim = _claim_attempt(lease, task.task_id, task.attempt_id, "test-finished-worker")
        assert claim is not None
        stopped_at = datetime.now(UTC)
        with session_factory.begin() as session:
            task_row = session.get(AnalysisTask, task.task_id)
            attempt = session.get(AnalysisAttempt, task.attempt_id)
            execution = session.scalar(
                select(AnalysisExecution).where(AnalysisExecution.attempt_id == task.attempt_id)
            )
            assert task_row is not None and attempt is not None and execution is not None
            task_row.status = "awaiting_summary"
            task_row.stage = "awaiting_summary"
            attempt.status = "awaiting_summary"
            attempt.manifest_fingerprint = "c" * 64
            attempt.stop_confirmed_at = stopped_at
            execution.stop_confirmed_at = stopped_at
            session.add(
                TranscriptSegment(
                    attempt_id=task.attempt_id,
                    segment_number=0,
                    audio_start_seconds=0,
                    audio_end_seconds=30,
                    actual_audio_seconds=30,
                    video_start_seconds=0,
                    video_end_seconds=30,
                    audio_sha256="a" * 64,
                    request_fingerprint="b" * 64,
                    status="succeeded",
                    text="persisted source transcript",
                    request_count=1,
                    request_metadata={"http_status": 200},
                )
            )
        lease.release()
        with session_factory() as session:
            attempt = session.get(AnalysisAttempt, task.attempt_id)
            video = session.get(Video, video_id)
            assert attempt is not None and video is not None
            work = store.analysis / task.task_id.hex / attempt.work_directory_name
            work.mkdir(parents=True)
            (work / "leftover.wav").write_bytes(b"leftover")
            original = store.target_path(video.storage_name)

        from tests.helpers import csrf, mutate_headers

        response = client.post(
            f"/videos/{video_id}/cleanup",
            json={"attempt_id": str(task.attempt_id)},
            headers=mutate_headers(csrf(client)),
        )
        assert response.status_code == 200
        assert response.json()["cleanup_state"] == "cleaned"
        assert not work.exists() and original.exists()
        transcript = client.get(f"/videos/{video_id}/transcript")
        assert transcript.status_code == 200
        assert transcript.json()["complete"] is True
        assert transcript.json()["items"][0]["text"] == "persisted source transcript"
        task_status = client.get(f"/videos/{video_id}/tasks/{task.task_id}")
        assert task_status.status_code == 200
        assert task_status.json()["recovery_eligible"] is False
        assert task_status.json()["recovery_error_code"] == "TRANSCRIPT_COMPLETE_AWAITING_SUMMARY"
        assert published == []


def test_checkpoint_matching_requires_the_entire_immutable_segment_identity() -> None:
    expected = {
        "segment_number": 2,
        "audio_start_seconds": 60.0,
        "audio_end_seconds": 62.0,
        "actual_audio_seconds": 2.0,
        "video_start_seconds": 60.0,
        "video_end_seconds": 62.0,
        "audio_sha256": "a" * 64,
        "request_fingerprint": "b" * 64,
    }
    previous = {**expected, "status": "succeeded", "text": "committed transcript"}
    assert checkpoint_matches(
        previous,
        expected,
        previous_manifest_fingerprint="c" * 64,
        expected_manifest_fingerprint="c" * 64,
    )
    assert not checkpoint_matches(
        previous,
        expected,
        previous_manifest_fingerprint="d" * 64,
        expected_manifest_fingerprint="c" * 64,
    )
    for key in expected:
        changed = {**expected, key: "changed"}
        assert not checkpoint_matches(
            previous,
            changed,
            previous_manifest_fingerprint="c" * 64,
            expected_manifest_fingerprint="c" * 64,
        )
    assert not checkpoint_matches(
        {**previous, "status": "expected", "text": None},
        expected,
        previous_manifest_fingerprint="c" * 64,
        expected_manifest_fingerprint="c" * 64,
    )


def test_recovery_copies_only_matching_committed_checkpoints(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-checkpoint-reuse")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    first_lease = ExecutionLease.acquire(test_engine)
    assert first_lease is not None
    first_claim = _claim_attempt(first_lease, submitted.task_id, submitted.attempt_id, "test-first-attempt")
    assert first_claim is not None
    config = first_claim.snapshot
    sample_rate = int(config["audio"]["sample_rate"])
    channels = int(config["audio"]["channels"])
    pcm = tmp_path / "full-audio.s16le"
    pcm.write_bytes(b"\x01\x00" * int(61.25 * sample_rate))
    entries = build_manifest(
        pcm_bytes=pcm.stat().st_size,
        duration_seconds=float(config["source"]["duration_seconds"]),
        sample_rate=sample_rate,
        channels=channels,
        segment_seconds=int(config["slicing"]["segment_seconds"]),
    )
    fingerprint_rows: list[dict[str, Any]] = []
    for entry in entries:
        wav_path = tmp_path / f"seed-{entry.segment_number:04d}.wav"
        with pcm.open("rb") as pcm_file:
            pcm_file.seek(entry.pcm_start_byte)
            audio_bytes = pcm_file.read(entry.pcm_end_byte - entry.pcm_start_byte)
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(audio_bytes)
        audio_sha256 = hashlib.sha256(wav_path.read_bytes()).hexdigest()
        request_fingerprint = canonical_sha256(
            {
                "version": ASR_REQUEST_FINGERPRINT_VERSION,
                "source_fingerprint": config["source"]["fingerprint"],
                "segment_number": entry.segment_number,
                "wav_sha256": audio_sha256,
                "wav_bytes": wav_path.stat().st_size,
                "model": config["asr"]["model"],
                "base_url": config["asr"]["base_url"],
                "parameters": config["asr"]["parameters"],
            }
        )
        fingerprint_rows.append(
            {
                "segment_number": entry.segment_number,
                "audio_start_seconds": entry.audio_start_seconds,
                "audio_end_seconds": entry.audio_end_seconds,
                "actual_audio_seconds": entry.actual_audio_seconds,
                "video_start_seconds": entry.video_start_seconds,
                "video_end_seconds": entry.video_end_seconds,
                "audio_sha256": audio_sha256,
                "request_fingerprint": request_fingerprint,
            }
        )
    manifest_fingerprint = canonical_sha256(
        {
            "version": config["slicing"]["manifest_version"],
            "timeline_mapping_version": config["slicing"]["timeline_mapping_version"],
            "audio": config["audio"],
            "source_fingerprint": config["source"]["fingerprint"],
            "segments": fingerprint_rows,
        }
    )
    stopped_at = datetime.now(UTC)
    with session_factory.begin() as session:
        parent = session.get(AnalysisAttempt, submitted.attempt_id)
        task = session.get(AnalysisTask, submitted.task_id)
        execution = session.scalar(
            select(AnalysisExecution).where(AnalysisExecution.attempt_id == submitted.attempt_id)
        )
        assert parent is not None and task is not None and execution is not None
        parent.status = "failed"
        parent.failure_code = "ASR_REQUEST_FAILED"
        parent.manifest_fingerprint = manifest_fingerprint
        task.status = "failed"
        task.stage = "failed"
        task.failure_code = "ASR_REQUEST_FAILED"
        execution.worker_instance_id = uuid.uuid4()
        execution.worker_container_id = "b" * 64
        execution.process_scope = PROCESS_SCOPE
        execution.boundary_pid = 333
        execution.boundary_start_ticks = 444
        execution.boundary_session_id = 333
        execution.boundary_boot_id = "test-checkpoint-boot"
        execution.supervisor_registered_at = stopped_at - timedelta(seconds=1)
        for index, row in enumerate(fingerprint_rows[:2]):
            session.add(
                TranscriptSegment(
                    attempt_id=parent.id,
                    segment_number=index,
                    audio_start_seconds=row["audio_start_seconds"],
                    audio_end_seconds=row["audio_end_seconds"],
                    actual_audio_seconds=row["actual_audio_seconds"],
                    video_start_seconds=row["video_start_seconds"],
                    video_end_seconds=row["video_end_seconds"],
                    audio_sha256=row["audio_sha256"],
                    request_fingerprint=(row["request_fingerprint"] if index == 0 else "f" * 64),
                    status="succeeded",
                    text=f"persisted segment {index}",
                    request_count=1,
                    request_metadata={"http_status": 200},
                )
            )
        row = fingerprint_rows[2]
        session.add(
            TranscriptSegment(
                attempt_id=parent.id,
                segment_number=2,
                audio_start_seconds=row["audio_start_seconds"],
                audio_end_seconds=row["audio_end_seconds"],
                actual_audio_seconds=row["actual_audio_seconds"],
                video_start_seconds=row["video_start_seconds"],
                video_end_seconds=row["video_end_seconds"],
                audio_sha256=row["audio_sha256"],
                request_fingerprint=row["request_fingerprint"],
                status="expected",
                request_count=1,
            )
        )
    with session_factory() as session:
        execution = session.get(AnalysisExecution, first_claim.execution_id)
        identity = stop_confirmation.identity_from_execution(execution) if execution is not None else None
        session.commit()
    assert identity is not None
    assert (
        confirm_stopped_execution(
            session_factory,
            worker_stop_evidence(identity, session_stopped=True),
        )
        == "confirmed"
    )
    first_lease.release()

    recovered = recover_analysis_attempt(
        session_factory, owner_id, video_id, submitted.task_id, submitted.attempt_id
    )
    second_lease = ExecutionLease.acquire(test_engine)
    assert second_lease is not None
    child_claim = _claim_attempt(
        second_lease, submitted.task_id, recovered.attempt_id, "test-recovery-attempt"
    )
    assert child_claim is not None
    maker = sessionmaker(bind=second_lease.connection, autoflush=False, expire_on_commit=False)
    _, reused = _save_manifest(maker, second_lease, child_claim, entries, pcm, config)
    assert reused == {0}
    with session_factory() as session:
        parent_segment = session.scalar(
            select(TranscriptSegment).where(
                TranscriptSegment.attempt_id == submitted.attempt_id,
                TranscriptSegment.segment_number == 0,
            )
        )
        child_segments = list(
            session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.attempt_id == recovered.attempt_id)
                .order_by(TranscriptSegment.segment_number)
            )
        )
        assert parent_segment is not None and parent_segment.text == "persisted segment 0"
        assert len(child_segments) == 3
        assert child_segments[0].status == "succeeded"
        assert child_segments[0].text == parent_segment.text
        assert child_segments[0].source_attempt_id == submitted.attempt_id
        assert child_segments[0].request_count == 0
        assert child_segments[1].status == "expected" and child_segments[1].text is None
        assert child_segments[2].status == "expected" and child_segments[2].text is None
        assert child_segments[0].audio_sha256 == fingerprint_rows[0]["audio_sha256"]
        assert child_segments[0].request_fingerprint == fingerprint_rows[0]["request_fingerprint"]
    second_lease.release()


def test_automatic_recovery_uses_two_task_level_slots_then_stops(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-auto-recovery-budget")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)

    for expected_count in (1, 2):
        _mark_attempt_failed_stopped(
            session_factory,
            submitted.task_id,
            submitted.attempt_id,
            failure_code="ASR_TIMEOUT",
        )
        cycle = coordinate_due_analysis_recoveries(
            session_factory,
            settings=test_settings,
            now=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert cycle.resumed == 1
        with session_factory() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            attempts = list(
                session.scalars(
                    select(AnalysisAttempt)
                    .where(AnalysisAttempt.task_id == submitted.task_id)
                    .order_by(AnalysisAttempt.attempt_number)
                )
            )
            assert task is not None
            assert task.auto_recovery_count == expected_count
            assert len(attempts) == expected_count + 1
            current_attempt_id = task.current_attempt_id
            parent_attempt_id = attempts[-2].id
            session.commit()
        if expected_count == 1:
            # A repeated scan and an explicit/manual race both return the child already created.
            repeated = coordinate_due_analysis_recoveries(
                session_factory,
                settings=test_settings,
                now=datetime.now(UTC) + timedelta(seconds=2),
            )
            assert repeated.resumed == 0
            manual = recover_analysis_attempt(
                session_factory,
                owner_id,
                video_id,
                submitted.task_id,
                parent_attempt_id,
                settings=test_settings,
            )
            assert manual.attempt_id == current_attempt_id
            assert not manual.created
        submitted = type(submitted)(submitted.task_id, current_attempt_id, created=False)

    _mark_attempt_failed_stopped(
        session_factory,
        submitted.task_id,
        submitted.attempt_id,
        failure_code="ASR_TIMEOUT",
    )
    exhausted = coordinate_due_analysis_recoveries(
        session_factory,
        settings=test_settings,
        now=datetime.now(UTC) + timedelta(seconds=3),
    )
    assert exhausted.resumed == 0
    assert exhausted.exhausted == 1
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        attempts = session.scalar(
            select(func.count())
            .select_from(AnalysisAttempt)
            .where(AnalysisAttempt.task_id == submitted.task_id)
        )
        assert task is not None
        assert task.auto_recovery_count == 2
        assert task.delivery_wait_reason == "automatic_recovery_exhausted"
        assert task.delivery_next_check_at is None
        assert attempts == 3


def test_automatic_recovery_waits_for_shared_cooldown_without_charging_budget(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-cooldown-wait")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    _mark_attempt_failed_stopped(
        session_factory,
        submitted.task_id,
        submitted.attempt_id,
        failure_code="ASR_RATE_LIMITED",
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    cooldown_until = now + timedelta(seconds=45)
    with session_factory.begin() as session:
        session.add(
            SharedModelCooldown(
                provider="siliconflow",
                account_reference=test_settings.siliconflow_account_reference,
                model=test_settings.asr_model,
                cooldown_until=cooldown_until,
                consecutive_backpressure_responses=1,
                last_backpressure_at=now,
            )
        )
    first_scan = coordinate_due_analysis_recoveries(
        session_factory,
        settings=test_settings,
        now=now,
    )
    assert first_scan.resumed == 0 and first_scan.waiting == 1
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None
        assert task.status == "waiting_model"
        assert task.model_wait_origin == "automatic_recovery"
        assert task.auto_recovery_count == 0
        assert task.model_wait_until == cooldown_until
        assert task.delivery_next_check_at == cooldown_until
        session.commit()

    next_check = cooldown_until + timedelta(seconds=1)
    with session_factory.begin() as session:
        cooldown = session.scalar(select(SharedModelCooldown))
        task = session.get(AnalysisTask, submitted.task_id)
        assert cooldown is not None and task is not None
        cooldown.cooldown_until = next_check - timedelta(seconds=1)
        task.delivery_next_check_at = cooldown_until
    resumed = coordinate_due_analysis_recoveries(
        session_factory,
        settings=test_settings,
        now=next_check,
    )
    assert resumed.resumed == 1
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None
        assert task.status == "queued"
        assert task.auto_recovery_count == 1
        assert task.model_wait_reason is None
        assert task.model_wait_total_seconds is not None and task.model_wait_total_seconds >= 45


def test_automatic_recovery_can_use_the_last_summary_request_and_stops_at_three(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-auto-summary-budget")
        video_ids = [
            _video(session_factory, store, owner_id),
            _video(session_factory, store, owner_id),
        ]

    def prepare_failed_summary_task(video_id: uuid.UUID, request_count: int) -> tuple[uuid.UUID, uuid.UUID]:
        task_id, attempt_id = _seed_stopped_complete_transcript(
            session_factory, owner_id, video_id, test_settings
        )
        request_started_at = datetime.now(UTC)
        with session_factory.begin() as session:
            task = session.get(AnalysisTask, task_id)
            attempt = session.get(AnalysisAttempt, attempt_id)
            assert task is not None and attempt is not None and attempt.execution_id is not None
            task.status = "failed"
            task.stage = "failed"
            task.summary_status = "failed"
            task.failure_code = "LLM_UPSTREAM_ERROR"
            task.delivery_wait_reason = "automatic_recovery_pending"
            task.delivery_next_check_at = request_started_at
            attempt.status = "failed"
            attempt.failure_code = "LLM_UPSTREAM_ERROR"
            for request_number in range(1, request_count + 1):
                session.add(
                    SummaryRequest(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        execution_id=attempt.execution_id,
                        request_number=request_number,
                        request_fingerprint=hashlib.sha256(
                            f"request-{task_id}-{request_number}".encode()
                        ).hexdigest(),
                        status="failed",
                        provider="test-local",
                        model="test-local",
                        prompt_version="stage5-test",
                        started_at=request_started_at,
                        finished_at=request_started_at,
                        error_code="LLM_UPSTREAM_ERROR",
                    )
                )
        return task_id, attempt_id

    task_id, parent_id = prepare_failed_summary_task(video_ids[0], request_count=2)
    cycle = coordinate_due_analysis_recoveries(
        session_factory,
        settings=test_settings,
        now=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert cycle.resumed == 1
    with session_factory() as session:
        task = session.get(AnalysisTask, task_id)
        child = session.scalar(select(AnalysisAttempt).where(AnalysisAttempt.parent_attempt_id == parent_id))
        assert task is not None and child is not None
        assert child.execution_mode == "summary_only"
        assert task.auto_recovery_count == 1
        request_count = session.scalar(
            select(func.count(SummaryRequest.id)).where(SummaryRequest.task_id == task_id)
        )
        assert request_count == 2
        child_id = child.id
        session.commit()

    monkeypatch.setattr(analysis_worker, "get_settings", lambda: test_settings)
    monkeypatch.setattr(analysis_worker, "_get_or_make_worker_components", lambda _: (test_engine, store))
    monkeypatch.setattr(
        analysis_worker,
        "_transcribe",
        lambda *_args, **_kwargs: pytest.fail("summary-only automatic recovery must not call ASR"),
    )
    _mock_llm_process(monkeypatch, [(_summary_model_output(), "stop")])
    assert transcribe_analysis(str(task_id), str(child_id)) == {"status": "succeeded"}
    with session_factory() as session:
        request_count = session.scalar(
            select(func.count(SummaryRequest.id)).where(SummaryRequest.task_id == task_id)
        )
        assert request_count == 3
        task = session.get(AnalysisTask, task_id)
        assert task is not None and task.status == "succeeded"

    exhausted_task_id, exhausted_parent_id = prepare_failed_summary_task(video_ids[1], request_count=3)
    exhausted = coordinate_due_analysis_recoveries(
        session_factory,
        settings=test_settings,
        now=datetime.now(UTC) + timedelta(seconds=2),
    )
    assert exhausted.resumed == 0 and exhausted.exhausted == 1
    with session_factory() as session:
        task = session.get(AnalysisTask, exhausted_task_id)
        child_count = session.scalar(
            select(func.count(AnalysisAttempt.id)).where(
                AnalysisAttempt.task_id == exhausted_task_id,
                AnalysisAttempt.parent_attempt_id == exhausted_parent_id,
            )
        )
        assert task is not None
        assert task.auto_recovery_count == 0
        assert task.delivery_wait_reason == "summary_request_budget_exhausted"
        assert task.failure_code == "LLM_UPSTREAM_ERROR"
        assert child_count == 0


def test_nonautomatic_wait_continuation_does_not_consume_recovery_budget(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-normal-wait")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    _mark_attempt_failed_stopped(
        session_factory,
        submitted.task_id,
        submitted.attempt_id,
        failure_code="ASR_RATE_LIMITED",
    )
    wait_until = datetime.now(UTC) + timedelta(seconds=30)
    wait_started = wait_until - timedelta(seconds=20)
    with session_factory.begin() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        attempt = session.get(AnalysisAttempt, submitted.attempt_id)
        assert task is not None and attempt is not None
        delivery_budget_before = (
            task.delivery_retry_count,
            task.delivery_retry_limit,
            task.delivery_operation_count,
            task.delivery_deadline_at,
        )
        task.status = "waiting_model"
        task.stage = "waiting_asr"
        task.model_wait_reason = "MODEL_COOLDOWN"
        task.model_wait_origin = "continuation"
        task.model_wait_stage = "transcribing"
        task.model_wait_started_at = wait_started
        task.model_wait_until = wait_until
        task.delivery_wait_reason = "shared_model_cooldown"
        task.delivery_next_check_at = wait_until
        task.queue_entered_at = None
        if task.delivery_deadline_at is not None and task.delivery_blocked_since is None:
            task.delivery_blocked_since = wait_started
        attempt.status = "waiting_model"
    result = create_followup_attempt(
        session_factory,
        task_id=submitted.task_id,
        video_id=video_id,
        parent_attempt_id=submitted.attempt_id,
        mode="wait_resume",
        settings=test_settings,
        now=wait_until + timedelta(seconds=1),
    )
    assert result.queued
    with session_factory() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        assert task is not None
        assert task.auto_recovery_count == 0
        assert task.status == "queued"
        assert (
            task.delivery_retry_count,
            task.delivery_retry_limit,
            task.delivery_operation_count,
        ) == delivery_budget_before[:3]
        if delivery_budget_before[3] is not None:
            assert task.delivery_deadline_at == delivery_budget_before[3] + timedelta(seconds=21)


def test_celery_delivery_and_acknowledgement_settings_are_explicit() -> None:
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_acks_late is False
    assert celery_app.conf.task_reject_on_worker_lost is False
    assert celery_app.conf.task_publish_retry is False
    assert celery_app.conf.worker_concurrency == 1
    assert celery_app.conf.worker_prefetch_multiplier == 1


def _seed_stopped_complete_transcript(
    factory: sessionmaker[Session], owner_id: uuid.UUID, video_id: uuid.UUID, settings: Settings
) -> tuple[uuid.UUID, uuid.UUID]:
    submitted = submit_analysis_task(factory, owner_id, video_id, settings)
    with factory() as session:
        attempt = session.get(AnalysisAttempt, submitted.attempt_id)
        video = session.get(Video, video_id)
        assert attempt is not None and video is not None
        snapshot = attempt.config_snapshot
        duration = video.duration_seconds
        session.commit()

    fingerprint_rows: list[dict[str, Any]] = []
    transcript_rows: list[TranscriptSegment] = []
    segment_seconds = int(snapshot["slicing"]["segment_seconds"])
    segment_count = int((duration + segment_seconds - 1) // segment_seconds)
    for number in range(segment_count):
        start = float(number * segment_seconds)
        end = float(min((number + 1) * segment_seconds, duration))
        audio_sha256 = hashlib.sha256(f"audio-{number}".encode()).hexdigest()
        request_fingerprint = hashlib.sha256(f"request-{number}".encode()).hexdigest()
        row = {
            "segment_number": number,
            "audio_start_seconds": start,
            "audio_end_seconds": end,
            "actual_audio_seconds": end - start,
            "video_start_seconds": start,
            "video_end_seconds": end,
            "audio_sha256": audio_sha256,
            "request_fingerprint": request_fingerprint,
        }
        fingerprint_rows.append(row)
        transcript_rows.append(
            TranscriptSegment(
                attempt_id=submitted.attempt_id,
                segment_number=number,
                audio_start_seconds=start,
                audio_end_seconds=end,
                actual_audio_seconds=end - start,
                video_start_seconds=start,
                video_end_seconds=end,
                audio_sha256=audio_sha256,
                request_fingerprint=request_fingerprint,
                status="succeeded",
                text=(
                    f"课程片段 {number}：FastAPI 通过依赖注入完成参数校验，"
                    "并使用 PostgreSQL 事务保存任务状态。"
                ),
                request_count=1,
                request_metadata={"http_status": 200, "elapsed_seconds": 0.1, "text_characters": 44},
            )
        )
    manifest_fingerprint = canonical_sha256(
        {
            "version": snapshot["slicing"]["manifest_version"],
            "timeline_mapping_version": snapshot["slicing"]["timeline_mapping_version"],
            "audio": snapshot["audio"],
            "source_fingerprint": snapshot["source"]["fingerprint"],
            "segments": fingerprint_rows,
        }
    )
    stopped_at = datetime.now(UTC)
    execution_id = uuid.uuid4()
    boundary_pid = 200_001
    with factory.begin() as session:
        task = session.get(AnalysisTask, submitted.task_id)
        attempt = session.get(AnalysisAttempt, submitted.attempt_id)
        assert task is not None and attempt is not None
        task.status = "awaiting_summary"
        task.stage = "awaiting_summary"
        task.summary_status = "waiting"
        attempt.status = "awaiting_summary"
        attempt.claim_count = 1
        attempt.execution_id = execution_id
        attempt.first_claimed_at = stopped_at
        attempt.stop_confirmed_at = stopped_at
        attempt.manifest_fingerprint = manifest_fingerprint
        session.add(
            AnalysisExecution(
                id=execution_id,
                task_id=task.id,
                attempt_id=attempt.id,
                worker_id="test-confirmed-stopped",
                backend_pid=1,
                worker_instance_id=uuid.uuid4(),
                worker_container_id="c" * 64,
                process_scope=PROCESS_SCOPE,
                boundary_pid=boundary_pid,
                boundary_start_ticks=987_321,
                boundary_session_id=boundary_pid,
                boundary_boot_id="test-summary-boot",
                supervisor_registered_at=stopped_at - timedelta(seconds=1),
            )
        )
        session.add_all(transcript_rows)
    with factory() as session:
        execution = session.get(AnalysisExecution, execution_id)
        identity = stop_confirmation.identity_from_execution(execution) if execution is not None else None
        session.commit()
    assert identity is not None
    assert (
        confirm_stopped_execution(
            factory,
            worker_stop_evidence(identity, session_stopped=True),
        )
        == "confirmed"
    )
    return submitted.task_id, submitted.attempt_id


def _summary_model_output() -> str:
    return json.dumps(
        {
            "chapters": [
                {"title": f"章节 {index}：FastAPI 依赖注入", "evidence": [{"segment_id": 0}]}
                for index in range(3)
            ],
            "key_points": [
                {"text": f"知识要点 {index}：PostgreSQL 事务", "evidence": [{"segment_id": 0}]}
                for index in range(3)
            ],
        },
        ensure_ascii=False,
    )


def _mock_llm_process(monkeypatch: pytest.MonkeyPatch, replies: list[tuple[str, str]]) -> None:
    calls = 0

    def run(command: list[str], **_: Any) -> None:
        nonlocal calls
        output_path = Path(command[command.index("--output") + 1])
        content, finish_reason = replies[min(calls, len(replies) - 1)]
        calls += 1
        output_path.write_text(
            json.dumps(
                {
                    "content": content,
                    "metadata": {
                        "elapsed_seconds": 0.25,
                        "response_bytes": len(content.encode("utf-8")),
                        "finish_reason": finish_reason,
                        "trace_id": "test-trace",
                        "usage": {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(analysis_worker, "_run_process", run)


def _mock_llm_error_process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error_code: str,
    retry_after: str,
    response_received_at: datetime,
) -> None:
    def run(command: list[str], **_: Any) -> None:
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "error_code": error_code,
                    "retry_after": retry_after,
                    "response_received_at": response_received_at.isoformat(),
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(analysis_worker, "_run_process", run)


def test_llm_overload_persists_shared_cooldown_before_automatic_recovery(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as client:
        owner_id = _register(client, "analysis-llm-overload-cooldown")
        video_id = _video(session_factory, store, owner_id)
    task_id, parent_id = _seed_stopped_complete_transcript(
        session_factory, owner_id, video_id, test_settings
    )
    followup = create_followup_attempt(
        session_factory,
        task_id=task_id,
        video_id=video_id,
        parent_attempt_id=parent_id,
        mode="manual_summary",
        owner_id=owner_id,
        settings=test_settings,
    )
    assert followup.queued
    attempt_id = followup.submission.attempt_id
    response_received_at = datetime.now(UTC)

    monkeypatch.setattr(analysis_worker, "get_settings", lambda: test_settings)
    monkeypatch.setattr(analysis_worker, "_get_or_make_worker_components", lambda _: (test_engine, store))
    monkeypatch.setattr(
        analysis_worker,
        "_transcribe",
        lambda *_args, **_kwargs: pytest.fail("summary-only recovery must not call ASR"),
    )
    _mock_llm_error_process(
        monkeypatch,
        error_code="LLM_MODEL_OVERLOADED",
        retry_after="300",
        response_received_at=response_received_at,
    )

    assert transcribe_analysis(str(task_id), str(attempt_id)) == {"status": "failed"}

    cooldown_until = response_received_at + timedelta(seconds=300)
    with session_factory() as session:
        cooldown = session.scalar(
            select(SharedModelCooldown).where(
                SharedModelCooldown.provider == "siliconflow",
                SharedModelCooldown.account_reference == test_settings.siliconflow_account_reference,
                SharedModelCooldown.model == test_settings.llm_model,
            )
        )
        request = session.scalar(
            select(SummaryRequest).where(
                SummaryRequest.task_id == task_id,
                SummaryRequest.attempt_id == attempt_id,
            )
        )
        assert cooldown is not None and cooldown.cooldown_until == cooldown_until
        assert cooldown.consecutive_backpressure_responses == 1
        assert request is not None and request.status == "failed"
        assert request.error_code == "LLM_MODEL_OVERLOADED"
        overload_event = session.scalar(
            select(AnalysisEvent).where(
                AnalysisEvent.task_id == task_id,
                AnalysisEvent.attempt_id == attempt_id,
                AnalysisEvent.event_type == "model_overloaded",
            )
        )
        assert overload_event is not None and overload_event.error_code == "LLM_MODEL_OVERLOADED"

    recovery = coordinate_due_analysis_recoveries(
        session_factory,
        settings=test_settings,
        now=response_received_at + timedelta(seconds=1),
    )
    assert recovery.resumed == 0 and recovery.waiting == 1
    with session_factory() as session:
        task = session.get(AnalysisTask, task_id)
        request_count = session.scalar(
            select(func.count(SummaryRequest.id)).where(SummaryRequest.task_id == task_id)
        )
        assert task is not None and task.status == "waiting_model"
        assert task.model_wait_until == cooldown_until
        assert task.auto_recovery_count == 0
        assert request_count == 1


def test_stopped_summary_continuation_publishes_once_without_asr(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    published: list[tuple[str, str]] = []
    app = create_app(
        test_settings,
        session_factory,
        store,
        run_recovery=False,
        task_publisher=lambda task_id, attempt_id: published.append((task_id, attempt_id)),
    )
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-summary-continuation")
        video_id = _video(session_factory, store, owner_id)
        task_id, parent_id = _seed_stopped_complete_transcript(
            session_factory, owner_id, video_id, test_settings
        )
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        endpoint = f"/videos/{video_id}/tasks/{task_id}/summarize"
        first = client.post(endpoint, json={"attempt_id": str(parent_id)}, headers=headers)
        assert first.status_code == 202, first.text
        child_id = uuid.UUID(first.json()["attempt"]["id"])
        assert first.json()["attempt"]["execution_mode"] == "summary_only"
        repeated = client.post(endpoint, json={"attempt_id": str(parent_id)}, headers=headers)
        assert repeated.status_code == 202
        assert repeated.json()["attempt"]["id"] == str(child_id)
        assert len(published) == 1

        monkeypatch.setattr(analysis_worker, "get_settings", lambda: test_settings)
        monkeypatch.setattr(analysis_worker, "_get_or_make_worker_components", lambda _: (test_engine, store))
        monkeypatch.setattr(
            analysis_worker,
            "_transcribe",
            lambda *_args, **_kwargs: pytest.fail("summary-only attempts must not call ASR"),
        )
        _mock_llm_process(monkeypatch, [(_summary_model_output(), "stop")])
        assert transcribe_analysis(str(task_id), str(child_id)) == {"status": "succeeded"}

        summary = client.get(f"/videos/{video_id}/summary")
        assert summary.status_code == 200
        payload = summary.json()["payload"]
        assert len(payload["chapters"]) == 3
        assert len(payload["key_points"]) == 3
        for item in payload["chapters"] + payload["key_points"]:
            evidence = item["evidence"][0]
            assert evidence["segment_id"] == 0
            assert evidence["quote"] in (
                "课程片段 0：FastAPI 通过依赖注入完成参数校验，并使用 PostgreSQL 事务保存任务状态。"
            )
            assert 0 <= evidence["start_seconds"] < evidence["end_seconds"] <= 61.25
        transcript = client.get(f"/videos/{video_id}/transcript")
        assert transcript.status_code == 200
        assert transcript.json()["complete"] is True
        assert all(item["source_attempt_id"] == str(parent_id) for item in transcript.json()["items"])

        with session_factory() as session:
            task = session.get(AnalysisTask, task_id)
            attempt = session.get(AnalysisAttempt, child_id)
            summary_row = session.scalar(select(AnalysisSummary).where(AnalysisSummary.task_id == task_id))
            requests = list(
                session.scalars(select(SummaryRequest).where(SummaryRequest.attempt_id == child_id))
            )
            assert task is not None and task.status == "succeeded" and task.summary_status == "succeeded"
            assert (
                attempt is not None and attempt.status == "succeeded" and attempt.cleanup_state == "cleaned"
            )
            assert summary_row is not None and len(requests) == 1 and requests[0].status == "succeeded"
            assert requests[0].finish_reason == "stop" and requests[0].usage == {
                "prompt_tokens": 123,
                "completion_tokens": 45,
                "total_tokens": 168,
            }

        with TestClient(create_app(test_settings, session_factory, store, run_recovery=False)) as stranger:
            stranger_id = _register(stranger, "analysis-summary-stranger")
            assert stranger_id != owner_id
            assert stranger.get(f"/videos/{video_id}/summary").status_code == 404


def test_truncated_summary_requires_manual_recovery_and_reuses_transcript(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    app = create_app(test_settings, session_factory, store, run_recovery=False)
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-summary-recovery")
        video_id = _video(session_factory, store, owner_id)
        task_id, parent_id = _seed_stopped_complete_transcript(
            session_factory, owner_id, video_id, test_settings
        )
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        continued = client.post(
            f"/videos/{video_id}/tasks/{task_id}/summarize",
            json={"attempt_id": str(parent_id)},
            headers=headers,
        )
        assert continued.status_code == 202, continued.text
        first_summary_attempt_id = uuid.UUID(continued.json()["attempt"]["id"])
        monkeypatch.setattr(analysis_worker, "get_settings", lambda: test_settings)
        monkeypatch.setattr(analysis_worker, "_get_or_make_worker_components", lambda _: (test_engine, store))
        monkeypatch.setattr(
            analysis_worker,
            "_transcribe",
            lambda *_args, **_kwargs: pytest.fail("summary recovery must not call ASR"),
        )
        _mock_llm_process(monkeypatch, [("{", "length"), ("{", "length"), (_summary_model_output(), "stop")])
        assert transcribe_analysis(str(task_id), str(first_summary_attempt_id)) == {"status": "failed"}
        assert client.get(f"/videos/{video_id}/summary").status_code == 409

        with session_factory() as session:
            first_requests = list(
                session.scalars(
                    select(SummaryRequest).where(SummaryRequest.attempt_id == first_summary_attempt_id)
                )
            )
            assert len(first_requests) == 1
            assert first_requests[0].status == "failed"
            assert first_requests[0].finish_reason == "length"
            assert first_requests[0].error_code == "LLM_OUTPUT_TRUNCATED"
            assert session.get(AnalysisTask, task_id).failure_code == "LLM_OUTPUT_TRUNCATED"
            assert (
                session.scalar(select(AnalysisSummary.id).where(AnalysisSummary.task_id == task_id)) is None
            )

        recovered = client.post(
            f"/videos/{video_id}/tasks/{task_id}/recover",
            json={"attempt_id": str(first_summary_attempt_id)},
            headers=headers,
        )
        assert recovered.status_code == 202, recovered.text
        retry_id = uuid.UUID(recovered.json()["attempt"]["id"])
        assert recovered.json()["attempt"]["execution_mode"] == "summary_only"
        assert recovered.json()["reused_segments"] == 3
        assert transcribe_analysis(str(task_id), str(retry_id)) == {"status": "failed"}
        assert client.get(f"/videos/{video_id}/summary").status_code == 409

        with session_factory() as session:
            second_requests = list(
                session.scalars(select(SummaryRequest).where(SummaryRequest.attempt_id == retry_id))
            )
            assert len(second_requests) == 1
            assert second_requests[0].request_number == 1
            assert second_requests[0].status == "failed"
            assert second_requests[0].error_code == "LLM_OUTPUT_TRUNCATED"

        recovered_again = client.post(
            f"/videos/{video_id}/tasks/{task_id}/recover",
            json={"attempt_id": str(retry_id)},
            headers=headers,
        )
        assert recovered_again.status_code == 202, recovered_again.text
        final_attempt_id = uuid.UUID(recovered_again.json()["attempt"]["id"])
        assert recovered_again.json()["attempt"]["execution_mode"] == "summary_only"
        assert recovered_again.json()["reused_segments"] == 3
        assert transcribe_analysis(str(task_id), str(final_attempt_id)) == {"status": "succeeded"}
        assert client.get(f"/videos/{video_id}/summary").status_code == 200

        with session_factory() as session:
            requests = list(
                session.scalars(
                    select(SummaryRequest)
                    .where(SummaryRequest.task_id == task_id)
                    .order_by(SummaryRequest.started_at)
                )
            )
            attempts = list(
                session.scalars(
                    select(AnalysisAttempt)
                    .where(AnalysisAttempt.task_id == task_id)
                    .order_by(AnalysisAttempt.created_at)
                )
            )
            assert len(requests) == 3
            assert [item.request_number for item in requests] == [1, 1, 1]
            assert [item.status for item in requests] == ["failed", "failed", "succeeded"]
            assert [item.finish_reason for item in requests] == ["length", "length", "stop"]
            assert [item.error_code for item in requests] == [
                "LLM_OUTPUT_TRUNCATED",
                "LLM_OUTPUT_TRUNCATED",
                None,
            ]
            assert len(attempts) == 4
            assert attempts[0].status == "awaiting_summary"
            assert attempts[1].status == "failed"
            assert attempts[2].status == "failed"
            assert attempts[3].status == "succeeded"
            assert all(attempt.execution_mode == "summary_only" for attempt in attempts[1:])
            task = session.get(AnalysisTask, task_id)
            assert task.status == "succeeded" and task.summary_status == "succeeded"
            summary = session.scalar(select(AnalysisSummary).where(AnalysisSummary.task_id == task_id))
            assert summary is not None


def test_publish_database_failure_is_not_visible_and_recovery_reuses_transcript(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    app = create_app(test_settings, session_factory, store, run_recovery=False)
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-summary-publish-failure")
        video_id = _video(session_factory, store, owner_id)
        task_id, parent_id = _seed_stopped_complete_transcript(
            session_factory, owner_id, video_id, test_settings
        )
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        continuation = client.post(
            f"/videos/{video_id}/tasks/{task_id}/summarize",
            json={"attempt_id": str(parent_id)},
            headers=headers,
        )
        first_id = uuid.UUID(continuation.json()["attempt"]["id"])
        monkeypatch.setattr(analysis_worker, "get_settings", lambda: test_settings)
        monkeypatch.setattr(analysis_worker, "_get_or_make_worker_components", lambda _: (test_engine, store))
        monkeypatch.setattr(
            analysis_worker,
            "_transcribe",
            lambda *_args, **_kwargs: pytest.fail("summary recovery must not call ASR"),
        )
        original_publish = analysis_worker._publish_summary

        def fail_publish(*_args: Any, **_kwargs: Any) -> None:
            raise SQLAlchemyError("injected publish failure")

        monkeypatch.setattr(analysis_worker, "_publish_summary", fail_publish)
        _mock_llm_process(monkeypatch, [(_summary_model_output(), "stop")])
        assert transcribe_analysis(str(task_id), str(first_id)) == {"status": "failed"}
        assert client.get(f"/videos/{video_id}/summary").status_code == 409
        with session_factory() as session:
            assert (
                session.scalar(select(AnalysisSummary.id).where(AnalysisSummary.task_id == task_id)) is None
            )
            request = session.scalar(select(SummaryRequest).where(SummaryRequest.attempt_id == first_id))
            assert request is not None
            assert request.status == "failed" and request.error_code == "SUMMARY_PUBLISH_FAILED"

        recovered = client.post(
            f"/videos/{video_id}/tasks/{task_id}/recover",
            json={"attempt_id": str(first_id)},
            headers=headers,
        )
        assert recovered.status_code == 202, recovered.text
        retry_id = uuid.UUID(recovered.json()["attempt"]["id"])
        assert recovered.json()["reused_segments"] == 3
        monkeypatch.setattr(analysis_worker, "_publish_summary", original_publish)
        _mock_llm_process(monkeypatch, [(_summary_model_output(), "stop")])
        assert transcribe_analysis(str(task_id), str(retry_id)) == {"status": "succeeded"}
        assert client.get(f"/videos/{video_id}/summary").status_code == 200


@pytest.mark.parametrize("deletion_kind", ["whole_record", "media_only"])
def test_delete_winning_summary_race_blocks_publication_and_later_requests(
    deletion_kind: str,
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    app = create_app(test_settings, session_factory, store, run_recovery=False)
    with TestClient(app) as client:
        owner_id = _register(client, "analysis-summary-delete-race")
        video_id = _video(session_factory, store, owner_id)
        task_id, parent_id = _seed_stopped_complete_transcript(
            session_factory, owner_id, video_id, test_settings
        )
        from tests.helpers import csrf, mutate_headers

        headers = mutate_headers(csrf(client))
        continuation = client.post(
            f"/videos/{video_id}/tasks/{task_id}/summarize",
            json={"attempt_id": str(parent_id)},
            headers=headers,
        )
        child_id = uuid.UUID(continuation.json()["attempt"]["id"])
        started = threading.Event()
        release = threading.Event()
        monkeypatch.setattr(analysis_worker, "get_settings", lambda: test_settings)
        monkeypatch.setattr(analysis_worker, "_get_or_make_worker_components", lambda _: (test_engine, store))
        monkeypatch.setattr(
            analysis_worker,
            "_transcribe",
            lambda *_args, **_kwargs: pytest.fail("summary-only attempts must not call ASR"),
        )

        def blocked_provider_call(command: list[str], **_: Any) -> None:
            output_path = Path(command[command.index("--output") + 1])
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test did not release the fake provider call")
            content = _summary_model_output()
            output_path.write_text(
                json.dumps(
                    {
                        "content": content,
                        "metadata": {
                            "elapsed_seconds": 0.25,
                            "response_bytes": len(content.encode("utf-8")),
                            "finish_reason": "stop",
                            "trace_id": "test-trace",
                            "usage": {},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        monkeypatch.setattr(analysis_worker, "_run_process", blocked_provider_call)
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(transcribe_analysis, str(task_id), str(child_id))
            assert started.wait(timeout=10)
            if deletion_kind == "media_only":
                assert delete_video_media(session_factory, store, owner_id, video_id) is False
            else:
                assert delete_video(session_factory, store, owner_id, video_id) == 202
            release.set()
            assert worker.result(timeout=15) == {"status": "failed"}

        assert client.get(f"/videos/{video_id}/summary").status_code == 409
        with session_factory() as session:
            assert (
                session.scalar(select(AnalysisSummary.id).where(AnalysisSummary.task_id == task_id)) is None
            )
            request = session.scalar(select(SummaryRequest).where(SummaryRequest.attempt_id == child_id))
            attempt = session.get(AnalysisAttempt, child_id)
            assert request is not None and request.status == "failed"
            assert attempt is not None and attempt.stop_confirmed_at is not None
            assert attempt.execution_mode == "summary_only"
            video = session.get(Video, video_id)
            if deletion_kind == "media_only":
                assert video is not None and video.status == "ready"
                assert video.media_status == "deletion_pending"
            else:
                assert video is not None and video.status in {"deleting", "cleanup_failed"}
        if deletion_kind == "media_only":
            retried = client.post(
                f"/videos/{video_id}/media-deletion/retry",
                headers=headers,
            )
            assert retried.status_code == 200
            assert retried.json()["media_status"] == "deleted"
            assert client.get(f"/videos/{video_id}/transcript").status_code == 200
            assert client.get(f"/videos/{video_id}/summary").status_code == 409
            assert client.get(f"/videos/{video_id}/content").status_code == 410
            assert client.delete(f"/videos/{video_id}", headers=headers).status_code == 204
        else:
            assert delete_video(session_factory, store, owner_id, video_id) == 204
        assert child_id != parent_id


def _claim_bound_execution(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    suffix: str,
) -> tuple[Any, ExecutionLease, WorkerBoundary]:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, f"stop-evidence-{suffix}")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    lease = ExecutionLease.acquire(test_engine)
    assert lease is not None
    boundary = WorkerBoundary(
        worker_instance_id=uuid.uuid4(),
        worker_container_id=uuid.uuid4().hex[:12],
        process_scope=PROCESS_SCOPE,
        boundary_pid=100_000 + int(uuid.uuid4().hex[:4], 16),
        boundary_start_ticks=987_654,
        boundary_session_id=100_000 + int(uuid.uuid4().hex[:4], 16),
        boundary_boot_id="isolated-test-boot",
    )
    boundary = replace(boundary, boundary_session_id=boundary.boundary_pid)
    claim = _claim_attempt(
        lease,
        submitted.task_id,
        submitted.attempt_id,
        f"test-supervised-{suffix}",
        boundary,
    )
    assert claim is not None
    lease.release()
    return claim, lease, boundary


def test_stop_confirmation_binds_identity_is_idempotent_and_preserves_request_budget(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    claim, _, _ = _claim_bound_execution(test_settings, test_engine, session_factory, "transaction")
    identity = analysis_worker._execution_identity(claim)
    with session_factory.begin() as session:
        execution = session.get(AnalysisExecution, claim.execution_id)
        task = session.get(AnalysisTask, claim.task_id)
        assert execution is not None and task is not None
    evidence = worker_stop_evidence(identity, session_stopped=True)
    assert confirm_stopped_execution(session_factory, evidence) == "rejected"

    with session_factory.begin() as session:
        execution = session.get(AnalysisExecution, claim.execution_id)
        task = session.get(AnalysisTask, claim.task_id)
        assert execution is not None and task is not None
        execution.supervisor_registered_at = datetime.now(UTC)
        task.summary_status = "running"
        session.add(
            SummaryRequest(
                task_id=claim.task_id,
                attempt_id=claim.attempt_id,
                execution_id=claim.execution_id,
                request_number=1,
                request_fingerprint="a" * 64,
                status="started",
                provider="test-local",
                model="test-local",
                prompt_version="test",
                started_at=datetime.now(UTC),
            )
        )

    wrong_generation = replace(identity, worker_instance_id=uuid.uuid4())
    wrong_evidence = worker_stop_evidence(wrong_generation, session_stopped=True)
    assert confirm_stopped_execution(session_factory, wrong_evidence) == "rejected"
    assert confirm_stopped_execution(session_factory, replace(evidence, sha256="0" * 64)) == "rejected"
    with session_factory() as session:
        execution = session.get(AnalysisExecution, claim.execution_id)
        assert execution is not None and execution.stop_confirmed_at is None

    assert confirm_stopped_execution(session_factory, evidence) == "confirmed"
    assert confirm_stopped_execution(session_factory, evidence) == "already_confirmed"
    with session_factory() as session:
        execution = session.get(AnalysisExecution, claim.execution_id)
        task = session.get(AnalysisTask, claim.task_id)
        attempt = session.get(AnalysisAttempt, claim.attempt_id)
        request = session.scalar(
            select(SummaryRequest).where(SummaryRequest.execution_id == claim.execution_id)
        )
        assert execution is not None and execution.stop_evidence_sha256 == evidence.sha256
        assert task is not None and task.status == "failed" and task.summary_status == "failed"
        assert attempt is not None and attempt.status == "failed" and attempt.cleanup_state == "pending"
        assert request is not None and request.status == "failed"
        assert (
            session.scalar(
                select(func.count(SummaryRequest.id)).where(SummaryRequest.task_id == claim.task_id)
            )
            == 1
        )


def test_confirmation_database_failure_keeps_the_execution_slot(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim, _, _ = _claim_bound_execution(test_settings, test_engine, session_factory, "write-failure")
    identity = analysis_worker._execution_identity(claim)
    with session_factory.begin() as session:
        execution = session.get(AnalysisExecution, claim.execution_id)
        assert execution is not None
        execution.supervisor_registered_at = datetime.now(UTC)
    evidence = worker_stop_evidence(identity, session_stopped=True)

    def fail_transaction(_factory: Any) -> Any:
        raise SQLAlchemyError("injected confirmation write failure")

    monkeypatch.setattr(stop_confirmation, "db_transaction", fail_transaction)
    with pytest.raises(SQLAlchemyError, match="injected confirmation write failure"):
        confirm_stopped_execution(session_factory, evidence)

    with session_factory() as session:
        execution = session.get(AnalysisExecution, claim.execution_id)
        assert execution is not None and execution.stop_confirmed_at is None
        assert execution.stop_evidence_sha256 is None
        assert (
            session.scalar(
                select(func.count(AnalysisExecution.id)).where(AnalysisExecution.stop_confirmed_at.is_(None))
            )
            == 1
        )


def test_late_confirmation_for_old_execution_cannot_release_new_execution_slot(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    old_claim, _, _ = _claim_bound_execution(test_settings, test_engine, session_factory, "old")
    old_identity = analysis_worker._execution_identity(old_claim)
    with session_factory.begin() as session:
        execution = session.get(AnalysisExecution, old_claim.execution_id)
        assert execution is not None
        execution.supervisor_registered_at = datetime.now(UTC)
    old_evidence = worker_stop_evidence(old_identity, session_stopped=True)
    assert confirm_stopped_execution(session_factory, old_evidence) == "confirmed"

    new_attempt_id = uuid.uuid4()
    with session_factory.begin() as session:
        old_attempt = session.get(AnalysisAttempt, old_claim.attempt_id)
        task = session.get(AnalysisTask, old_claim.task_id)
        assert old_attempt is not None and task is not None
        successor = AnalysisAttempt(
            id=new_attempt_id,
            task_id=task.id,
            attempt_number=old_attempt.attempt_number + 1,
            status="queued",
            execution_mode="full",
            config_snapshot=old_attempt.config_snapshot,
            config_sha256=old_attempt.config_sha256,
            source_fingerprint=old_attempt.source_fingerprint,
            work_directory_name=uuid.uuid4().hex,
            claim_count=0,
            cleanup_state="pending",
        )
        session.add(successor)
        session.flush()
        task.current_attempt_id = successor.id
        task.status = "queued"
        task.stage = "queued"
        task.failure_code = None
        session.add(TaskDelivery(attempt_id=successor.id, status="pending"))

    lease = ExecutionLease.acquire(test_engine)
    assert lease is not None
    new_boundary = WorkerBoundary(
        worker_instance_id=uuid.uuid4(),
        worker_container_id=uuid.uuid4().hex[:12],
        process_scope=PROCESS_SCOPE,
        boundary_pid=200_000 + int(uuid.uuid4().hex[:4], 16),
        boundary_start_ticks=123_456,
        boundary_session_id=1,
        boundary_boot_id="isolated-test-boot-new",
    )
    new_boundary = replace(new_boundary, boundary_session_id=new_boundary.boundary_pid)
    new_claim = _claim_attempt(
        lease,
        old_claim.task_id,
        new_attempt_id,
        "test-supervised-new",
        new_boundary,
    )
    lease.release()
    assert new_claim is not None
    wrong_execution_identity = replace(
        analysis_worker._execution_identity(new_claim),
        execution_id=old_claim.execution_id,
    )
    wrong_execution_evidence = worker_stop_evidence(wrong_execution_identity, session_stopped=True)
    assert confirm_stopped_execution(session_factory, wrong_execution_evidence) == "rejected"
    assert confirm_stopped_execution(session_factory, old_evidence) == "already_confirmed"
    with session_factory() as session:
        unconfirmed = list(
            session.scalars(select(AnalysisExecution.id).where(AnalysisExecution.stop_confirmed_at.is_(None)))
        )
        assert unconfirmed == [new_claim.execution_id]
        new_execution = session.get(AnalysisExecution, new_claim.execution_id)
        assert new_execution is not None and new_execution.stop_confirmed_at is None


def test_confirmation_preserves_a_published_terminal_result(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    claim, _, _ = _claim_bound_execution(test_settings, test_engine, session_factory, "published")
    identity = analysis_worker._execution_identity(claim)
    with session_factory.begin() as session:
        execution = session.get(AnalysisExecution, claim.execution_id)
        task = session.get(AnalysisTask, claim.task_id)
        attempt = session.get(AnalysisAttempt, claim.attempt_id)
        assert execution is not None and task is not None and attempt is not None
        execution.supervisor_registered_at = datetime.now(UTC)
        task.status = "succeeded"
        task.stage = "succeeded"
        task.summary_status = "succeeded"
        attempt.status = "succeeded"
        session.add(
            AnalysisSummary(
                task_id=claim.task_id,
                video_id=claim.video_id,
                attempt_id=claim.attempt_id,
                manifest_fingerprint="a" * 64,
                transcript_fingerprint="b" * 64,
                structure_version="test-v1",
                provider="test-local",
                model="test-local",
                prompt_version="test-v1",
                prompt_sha256="c" * 64,
                response_schema_sha256="d" * 64,
                payload_sha256="e" * 64,
                payload={"preserved": True},
            )
        )

    evidence = worker_stop_evidence(identity, session_stopped=True)
    assert confirm_stopped_execution(session_factory, evidence) == "confirmed"
    with session_factory() as session:
        task = session.get(AnalysisTask, claim.task_id)
        attempt = session.get(AnalysisAttempt, claim.attempt_id)
        summary = session.scalar(select(AnalysisSummary).where(AnalysisSummary.task_id == claim.task_id))
        assert task is not None and task.status == "succeeded" and task.stage == "succeeded"
        assert task.summary_status == "succeeded"
        assert attempt is not None and attempt.status == "succeeded"
        assert summary is not None and summary.payload == {"preserved": True}


def test_legacy_execution_without_new_identity_cannot_be_confirmed_from_operator_claim(
    test_settings: Settings,
    test_engine: Any,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(create_app(test_settings, session_factory, run_recovery=False)) as client:
        owner_id = _register(client, "stop-evidence-legacy")
        video_id = _video(session_factory, store, owner_id)
    submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
    lease = ExecutionLease.acquire(test_engine)
    assert lease is not None
    claim = _claim_attempt(lease, submitted.task_id, submitted.attempt_id, "legacy-worker")
    assert claim is not None
    lease.release()
    with session_factory() as session:
        execution = session.get(AnalysisExecution, claim.execution_id)
        assert execution is not None
        with pytest.raises(BoundaryUnknown):
            analysis_worker._execution_identity(claim)
        assert execution.worker_instance_id is None
        assert execution.stop_confirmed_at is None
    assert (
        confirm_operator_container_stop(
            session_factory,
            claim.execution_id,
            uuid.uuid4(),
            "a" * 64,
            container_exited=True,
        )
        == "rejected"
    )
