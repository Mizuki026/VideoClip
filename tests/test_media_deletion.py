from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

import videoclip.analysis_worker as analysis_worker
import videoclip.services as video_services
from tests.helpers import csrf, login, mutate_headers, register
from videoclip.analysis import submit_analysis_task
from videoclip.analysis_worker import AnalysisFailure, ExecutionLease, _claim_attempt
from videoclip.events import record_analysis_event
from videoclip.main import create_app
from videoclip.models import (
    AnalysisAttempt,
    AnalysisExecution,
    AnalysisSummary,
    AnalysisTask,
    CleanupIntent,
    SharedModelCooldown,
    SummaryRequest,
    TaskDelivery,
    TranscriptSegment,
    UploadHandoff,
    Video,
)
from videoclip.services import delete_video_media
from videoclip.settings import Settings
from videoclip.storage import MediaStore
from videoclip.summarization import resolve_summary_contract

PASSWORD = "correct horse battery staple 42"


def _account(client: TestClient, username: str) -> uuid.UUID:
    created = register(client, username, PASSWORD)
    assert created.status_code == 201
    assert login(client, username, PASSWORD).status_code == 200
    return uuid.UUID(created.json()["id"])


def _video(factory: sessionmaker[Session], store: MediaStore, owner_id: uuid.UUID) -> uuid.UUID:
    upload_id = uuid.uuid4()
    storage_name = uuid.uuid4().hex
    content = b"stage-six-media"
    upload = UploadHandoff(
        id=upload_id,
        owner_id=owner_id,
        writer_id=uuid.uuid4(),
        part_name=f"{upload_id.hex}.part",
        target_name=storage_name,
        display_name="stage-six.mp4",
        state="complete",
        byte_count=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        duration_seconds=30,
        media_info={
            "container": "mp4",
            "timeline_start_seconds": 0,
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
        display_name="stage-six.mp4",
        storage_name=storage_name,
        byte_count=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        duration_seconds=30,
        media_info=upload.media_info,
        status="ready",
    )
    with factory.begin() as session:
        session.add_all([upload, video])
    store.prepare()
    store.target_path(storage_name).write_bytes(content)
    return video.id


def _claimed_and_confirmed(factory: sessionmaker[Session], task_id: uuid.UUID, attempt_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    execution_id = uuid.uuid4()
    with factory.begin() as session:
        task = session.get(AnalysisTask, task_id)
        attempt = session.get(AnalysisAttempt, attempt_id)
        assert task is not None and attempt is not None
        task.status = "running"
        task.stage = "transcribing"
        attempt.status = "running"
        attempt.claim_count = 1
        attempt.execution_id = execution_id
        attempt.first_claimed_at = now
        attempt.stop_confirmed_at = now
        session.add(
            AnalysisExecution(
                id=execution_id,
                task_id=task_id,
                attempt_id=attempt_id,
                worker_id="stage-six-test-worker",
                backend_pid=1,
                worker_instance_id=uuid.uuid4(),
                worker_container_id="a" * 64,
                process_scope="linux_session_v1",
                boundary_pid=1234,
                boundary_start_ticks=4321,
                boundary_session_id=1234,
                boundary_boot_id="stage-six-test-boot",
                supervisor_registered_at=now,
                stop_confirmed_at=now,
                stop_confirmation_source="test_fixture",
                stop_reason="fixture_complete",
            )
        )


def _add_transcript(factory: sessionmaker[Session], attempt_id: uuid.UUID, *, complete: bool) -> None:
    with factory.begin() as session:
        attempt = session.get(AnalysisAttempt, attempt_id)
        assert attempt is not None
        if complete:
            attempt.manifest_fingerprint = "a" * 64
        session.add(
            TranscriptSegment(
                attempt_id=attempt_id,
                segment_number=0,
                audio_start_seconds=0,
                audio_end_seconds=12,
                actual_audio_seconds=12,
                video_start_seconds=2,
                video_end_seconds=14,
                audio_sha256="b" * 64,
                request_fingerprint="c" * 64,
                status="succeeded",
                text="保留的转写原文",
                request_count=1,
            )
        )


def _add_published_summary(
    factory: sessionmaker[Session], video_id: uuid.UUID, task_id: uuid.UUID, attempt_id: uuid.UUID
) -> None:
    with factory.begin() as session:
        task = session.get(AnalysisTask, task_id)
        attempt = session.get(AnalysisAttempt, attempt_id)
        assert task is not None and attempt is not None
        task.status = "succeeded"
        task.stage = "succeeded"
        task.summary_status = "succeeded"
        attempt.status = "succeeded"
        session.add(
            AnalysisSummary(
                task_id=task_id,
                video_id=video_id,
                attempt_id=attempt_id,
                manifest_fingerprint="a" * 64,
                transcript_fingerprint="b" * 64,
                structure_version="stage-six-test-v1",
                provider="local-stub",
                model="local-stub",
                prompt_version="stage-six-test-v1",
                prompt_sha256="c" * 64,
                response_schema_sha256="d" * 64,
                payload_sha256="e" * 64,
                payload={"chapters": [], "key_points": [], "preserved": True},
            )
        )


def _app(test_settings: Settings, factory: sessionmaker[Session], store: MediaStore):
    return create_app(test_settings, factory, store, run_recovery=False)


def test_media_delete_keeps_published_notes_and_closes_every_new_media_method(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(_app(test_settings, session_factory, store)) as client:
        owner_id = _account(client, "media-delete-published")
        video_id = _video(session_factory, store, owner_id)
        submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
        _claimed_and_confirmed(session_factory, submitted.task_id, submitted.attempt_id)
        _add_transcript(session_factory, submitted.attempt_id, complete=True)
        _add_published_summary(session_factory, video_id, submitted.task_id, submitted.attempt_id)
        headers = mutate_headers(csrf(client))

        removed = client.delete(f"/videos/{video_id}/media", headers=headers)
        assert removed.status_code == 200
        assert removed.json()["media_status"] == "deleted"
        assert client.get(f"/videos/{video_id}/content").status_code == 410
        assert client.head(f"/videos/{video_id}/content").status_code == 410
        assert client.get(f"/videos/{video_id}/content", headers={"Range": "bytes=0-2"}).status_code == 410
        transcript = client.get(f"/videos/{video_id}/transcript")
        summary = client.get(f"/videos/{video_id}/summary")
        timeline = client.get(f"/videos/{video_id}/events?limit=2")
        assert transcript.status_code == 200
        assert transcript.json()["complete"] is True
        assert transcript.json()["items"][0]["text"] == "保留的转写原文"
        assert summary.status_code == 200
        assert summary.json()["attempt_id"] == str(submitted.attempt_id)
        assert summary.json()["payload"]["preserved"] is True
        assert timeline.status_code == 200
        assert timeline.headers["cache-control"] == "private, no-store"
        assert timeline.json()["has_more"] is True
        assert [item["event_type"] for item in timeline.json()["items"]] == [
            "attempt_cleanup_completed",
            "media_cleanup_completed",
        ]
        earlier_timeline = client.get(
            f"/videos/{video_id}/events?before_id={timeline.json()['next_before_id']}&limit=2"
        )
        assert earlier_timeline.status_code == 200
        assert [item["event_type"] for item in earlier_timeline.json()["items"]] == [
            "media_cleanup_started",
            "attempt_cleanup_started",
        ]
        assert client.get(f"/videos/{video_id}").json()["media_status"] == "deleted"
        assert client.get(f"/videos/{video_id}/media-deletion").json()["media_status"] == "deleted"
        repeated = client.delete(f"/videos/{video_id}/media", headers=headers)
        assert repeated.status_code == 200
        assert repeated.json()["media_status"] == "deleted"
        with session_factory() as session:
            storage_name = session.scalar(select(Video.storage_name).where(Video.id == video_id))
        assert storage_name is not None and not store.target_path(storage_name).exists()
        assert client.delete(f"/videos/{video_id}", headers=headers).status_code == 204
        assert client.get(f"/videos/{video_id}").status_code == 404
        assert client.get(f"/videos/{video_id}/transcript").status_code == 404
        assert client.get(f"/videos/{video_id}/events").status_code == 404


def test_event_timeline_is_idempotent_paginated_and_owner_scoped(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    app = _app(test_settings, session_factory, store)
    with TestClient(app) as owner_client, TestClient(app) as other_client:
        owner_id = _account(owner_client, "analysis-events-owner")
        video_id = _video(session_factory, store, owner_id)
        submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
        with session_factory.begin() as session:
            event = record_analysis_event(
                session,
                task_id=submitted.task_id,
                event_key="test-checkpoint-1",
                event_type="checkpoint_saved",
                attempt_id=submitted.attempt_id,
                request_reference=f"transcript-segment:{submitted.attempt_id}:1",
                stage="transcribing",
            )
            replay = record_analysis_event(
                session,
                task_id=submitted.task_id,
                event_key="test-checkpoint-1",
                event_type="checkpoint_saved",
                attempt_id=submitted.attempt_id,
                request_reference=f"transcript-segment:{submitted.attempt_id}:1",
                stage="transcribing",
            )
            assert event is not None and replay is not None
            assert event.id == replay.id
            record_analysis_event(
                session,
                task_id=submitted.task_id,
                event_key="test-summary-request-1",
                event_type="summary_requested",
                attempt_id=submitted.attempt_id,
                request_reference=f"summary-request:{uuid.uuid4()}",
                stage="summarizing",
            )

        first = owner_client.get(f"/videos/{video_id}/events?limit=2")
        assert first.status_code == 200
        payload = first.json()
        assert payload["has_more"] is True
        assert [item["event_type"] for item in payload["items"]] == [
            "checkpoint_saved",
            "summary_requested",
        ]
        older = owner_client.get(
            f"/videos/{video_id}/events?limit=2&before_id={payload['next_before_id']}"
        )
        assert older.status_code == 200
        assert [item["event_type"] for item in older.json()["items"]] == ["analysis_submitted"]
        assert older.json()["has_more"] is False
        assert first.json()["items"][0]["request_reference"].startswith("transcript-segment:")

        other_id = _account(other_client, "analysis-events-other")
        assert other_id != owner_id
        assert other_client.get(f"/videos/{video_id}/events").status_code == 404
        assert owner_client.get(f"/videos/{video_id}/events?limit=101").status_code == 422


def test_deleting_awaiting_summary_cancels_plan_without_publishing_a_summary(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(_app(test_settings, session_factory, store)) as client:
        owner_id = _account(client, "media-delete-awaiting-summary")
        video_id = _video(session_factory, store, owner_id)
        submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
        _add_transcript(session_factory, submitted.attempt_id, complete=True)
        with session_factory.begin() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            attempt = session.get(AnalysisAttempt, submitted.attempt_id)
            delivery = session.get(TaskDelivery, submitted.attempt_id)
            assert task is not None and attempt is not None and delivery is not None
            task.status = "awaiting_summary"
            task.stage = "awaiting_summary"
            task.summary_status = "waiting"
            task.delivery_next_check_at = datetime.now(UTC)
            task.auto_recovery_count = 1
            task.summary_requests_used = 1
            attempt.status = "awaiting_summary"
            delivery.status = "sent"
            delivery.operation_generation = 7
        with session_factory() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            assert task is not None
            before = (
                task.auto_recovery_count,
                task.delivery_retry_count,
                task.delivery_operation_count,
            )
            before_summary_requests = session.scalar(
                select(func.count(SummaryRequest.id)).where(SummaryRequest.task_id == task.id)
            )

        removed = client.delete(f"/videos/{video_id}/media", headers=mutate_headers(csrf(client)))
        assert removed.status_code == 200
        assert removed.json()["media_status"] == "deleted"
        assert client.get(f"/videos/{video_id}/transcript").json()["complete"] is True
        assert client.get(f"/videos/{video_id}/summary").status_code == 409
        headers = mutate_headers(csrf(client))
        for action in ("recover", "summarize"):
            rejected = client.post(
                f"/videos/{video_id}/tasks/{submitted.task_id}/{action}",
                headers=headers,
                json={"attempt_id": str(submitted.attempt_id)},
            )
            assert rejected.status_code == 409
            assert rejected.json()["detail"]["code"] == "VIDEO_MEDIA_UNAVAILABLE"
        with session_factory() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            attempt = session.get(AnalysisAttempt, submitted.attempt_id)
            delivery = session.get(TaskDelivery, submitted.attempt_id)
            summaries = list(
                session.scalars(select(AnalysisSummary).where(AnalysisSummary.task_id == submitted.task_id))
            )
            assert task is not None and attempt is not None and delivery is not None
            assert task.status == "failed" and task.failure_code == "VIDEO_MEDIA_DELETION_REQUESTED"
            assert task.delivery_next_check_at is None and task.model_wait_until is None
            assert (
                task.auto_recovery_count,
                task.delivery_retry_count,
                task.delivery_operation_count,
            ) == before
            assert (
                session.scalar(select(func.count(SummaryRequest.id)).where(SummaryRequest.task_id == task.id))
                == before_summary_requests
            )
            assert attempt.status == "failed" and attempt.stop_confirmed_at is None
            assert delivery.status == "failed" and delivery.operation_generation == 8
            assert summaries == []


@pytest.mark.parametrize("plan", ["queued", "shared_cooldown", "automatic_recovery_pending"])
def test_media_delete_invalidates_queued_wait_and_automatic_recovery_plans(
    plan: str,
    test_settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(_app(test_settings, session_factory, store)) as client:
        username = f"media-s6-{plan.replace('_', '-')[:15]}-{uuid.uuid4().hex[:4]}"
        owner_id = _account(client, username)
        video_id = _video(session_factory, store, owner_id)
        submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
        due = datetime.now(UTC) + timedelta(minutes=5)
        cooldown_id: uuid.UUID | None = None
        with session_factory.begin() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            attempt = session.get(AnalysisAttempt, submitted.attempt_id)
            assert task is not None and attempt is not None
            if plan == "shared_cooldown":
                task.status = "waiting_model"
                task.stage = "waiting_asr"
                task.delivery_next_check_at = due
                task.model_wait_reason = "MODEL_COOLDOWN"
                task.model_wait_provider = "siliconflow"
                task.model_wait_account_reference = "stage-six-shared-account"
                task.model_wait_model = "stage-six-model"
                task.model_wait_stage = "transcribing"
                task.model_wait_origin = "automatic_recovery"
                task.model_wait_until = due
                task.model_wait_single_deadline_at = due + timedelta(hours=1)
                attempt.status = "waiting_model"
                cooldown = SharedModelCooldown(
                    provider="siliconflow",
                    account_reference="stage-six-shared-account",
                    model="stage-six-model",
                    cooldown_until=due,
                    consecutive_rate_limits=1,
                    last_rate_limited_at=datetime.now(UTC),
                )
                session.add(cooldown)
                session.flush()
                cooldown_id = cooldown.id
            elif plan == "automatic_recovery_pending":
                task.status = "failed"
                task.stage = "failed"
                task.failure_code = "ASR_RATE_LIMITED"
                task.delivery_wait_reason = "automatic_recovery_pending"
                task.delivery_next_check_at = due
                task.auto_recovery_count = 1
                task.auto_recovery_limit = 2
                attempt.status = "failed"
                attempt.failure_code = "ASR_RATE_LIMITED"
            else:
                task.delivery_next_check_at = due
                task.delivery_wait_reason = "queued_for_delivery"
            task.delivery_retry_count = 2
            task.delivery_operation_count = 4
            task.auto_recovery_count = task.auto_recovery_count or 0

        removed = client.delete(
            f"/videos/{video_id}/media", headers=mutate_headers(csrf(client))
        )
        assert removed.status_code == 200
        with session_factory() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            attempt = session.get(AnalysisAttempt, submitted.attempt_id)
            delivery = session.get(TaskDelivery, submitted.attempt_id)
            assert task is not None and attempt is not None and delivery is not None
            assert task.status == "failed"
            assert task.failure_code == "VIDEO_MEDIA_DELETION_REQUESTED"
            assert task.delivery_next_check_at is None
            assert task.model_wait_until is None and task.model_wait_single_deadline_at is None
            assert task.delivery_retry_count == 2 and task.delivery_operation_count == 4
            assert attempt.status == "failed" and attempt.stop_confirmed_at is None
            assert delivery.status == "failed"
            if plan == "automatic_recovery_pending":
                assert task.auto_recovery_count == 1
            if cooldown_id is not None:
                cooldown = session.get(SharedModelCooldown, cooldown_id)
                assert cooldown is not None and cooldown.cooldown_until == due


def test_media_deletion_serializes_with_task_submission_and_late_worker_claim(
    test_settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(_app(test_settings, session_factory, store)) as client:
        owner_id = _account(client, "media-delete-submit-race")
    video_id = _video(session_factory, store, owner_id)
    start = Barrier(2)

    def submit() -> tuple[str, object]:
        start.wait(timeout=10)
        try:
            return "submitted", submit_analysis_task(session_factory, owner_id, video_id, test_settings)
        except HTTPException as exc:
            return "rejected", exc.status_code

    def remove_media() -> bool:
        start.wait(timeout=10)
        return delete_video_media(session_factory, store, owner_id, video_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        submission_future = pool.submit(submit)
        deletion_future = pool.submit(remove_media)
        outcome, value = submission_future.result(timeout=20)
        assert deletion_future.result(timeout=20) is True

    with session_factory() as session:
        video = session.get(Video, video_id)
        task = session.scalar(select(AnalysisTask).where(AnalysisTask.video_id == video_id))
        attempts = list(
            session.scalars(
                select(AnalysisAttempt)
                .join(AnalysisTask, AnalysisAttempt.task_id == AnalysisTask.id)
                .where(AnalysisTask.video_id == video_id)
            )
        )
        assert video is not None and video.media_status == "deleted"
        if outcome == "rejected":
            assert value == 409 and task is None and attempts == []
        else:
            assert task is not None and task.status == "failed"
            assert len(attempts) == 1 and attempts[0].status == "failed"
            engine = session_factory.kw["bind"]
            assert engine is not None
            lease = ExecutionLease.acquire(engine)
            assert lease is not None
            try:
                assert _claim_attempt(lease, task.id, attempts[0].id, "late-media-delete-message") is None
            finally:
                lease.release()


def test_media_delete_blocks_late_checkpoint_response_and_summary_publication(
    test_settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(_app(test_settings, session_factory, store)) as client:
        owner_id = _account(client, "media-delete-late-write")
        video_id = _video(session_factory, store, owner_id)
        submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
        with session_factory.begin() as session:
            attempt = session.get(AnalysisAttempt, submitted.attempt_id)
            assert attempt is not None
            attempt.execution_mode = "summary_only"

        engine = session_factory.kw["bind"]
        assert engine is not None
        lease = ExecutionLease.acquire(engine)
        assert lease is not None
        try:
            claim = _claim_attempt(lease, submitted.task_id, submitted.attempt_id, "late-write-gate")
            assert claim is not None
            maker = sessionmaker(bind=lease.connection, autoflush=False, expire_on_commit=False)
            contract = resolve_summary_contract(claim.snapshot)
            request = analysis_worker._start_summary_request(
                maker,
                lease,
                claim,
                contract,
                "a" * 64,
                settings=test_settings,
                correction_error_code=None,
            )

            assert delete_video_media(session_factory, store, owner_id, video_id) is False
            late_writes = (
                lambda: analysis_worker._persist_transcript(
                    maker, lease, claim, 0, "迟到检查点", {"response_bytes": 18}
                ),
                lambda: analysis_worker._record_summary_response(
                    maker,
                    lease,
                    claim,
                    request.id,
                    {"elapsed_seconds": 0.1, "response_bytes": 18},
                ),
                lambda: analysis_worker._publish_summary(
                    maker,
                    lease,
                    claim,
                    request.id,
                    contract=contract,
                    expected_transcript_fingerprint="b" * 64,
                    payload={"chapters": [], "key_points": []},
                ),
            )
            for late_write in late_writes:
                with pytest.raises(AnalysisFailure) as rejected:
                    late_write()
                assert rejected.value.code == "EXECUTION_OWNERSHIP_LOST"
        finally:
            lease.release()

        assert client.get(f"/videos/{video_id}/transcript").status_code == 200
        assert client.get(f"/videos/{video_id}/summary").status_code == 409
        with session_factory() as session:
            request = session.get(SummaryRequest, request.id)
            summary = session.scalar(
                select(AnalysisSummary).where(AnalysisSummary.task_id == submitted.task_id)
            )
            assert request is not None and request.status == "failed"
            assert summary is None


def test_media_open_and_delete_serialize_before_file_open(
    test_settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(_app(test_settings, session_factory, store)) as client:
        owner_id = _account(client, "media-open-delete-lock")
        video_id = _video(session_factory, store, owner_id)
        with session_factory() as session:
            storage_name = session.scalar(select(Video.storage_name).where(Video.id == video_id))
        assert storage_name is not None
        media_path = store.target_path(storage_name)
        original_open = Path.open
        open_entered = Event()
        release_open = Event()
        delete_started = Event()
        delete_finished = Event()

        def pause_target_open(path: Path, *args: object, **kwargs: object):
            if path == media_path:
                open_entered.set()
                assert release_open.wait(timeout=10)
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", pause_target_open)

        def delete_media():
            delete_started.set()
            try:
                return client.delete(
                    f"/videos/{video_id}/media",
                    headers=mutate_headers(csrf(client)),
                )
            finally:
                delete_finished.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            head_future = pool.submit(client.head, f"/videos/{video_id}/content")
            assert open_entered.wait(timeout=10)
            delete_future = pool.submit(delete_media)
            assert delete_started.wait(timeout=10)
            time.sleep(0.1)
            assert not delete_finished.is_set()
            release_open.set()
            head = head_future.result(timeout=15)
            deleted = delete_future.result(timeout=15)

        assert head.status_code == 200
        assert deleted.status_code == 200
        assert deleted.json()["media_status"] == "deleted"
        assert client.head(f"/videos/{video_id}/content").status_code == 410


def test_missing_media_after_state_commit_failure_recovers_idempotently(
    test_settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    original_transaction = video_services.db_transaction
    media_path_holder: list[Path] = []

    @contextmanager
    def fail_after_file_unlink(factory: sessionmaker[Session]):
        with original_transaction(factory) as session:
            yield session
            if media_path_holder and not media_path_holder[0].exists():
                raise SQLAlchemyError("injected post-unlink state commit failure")

    monkeypatch.setattr(video_services, "db_transaction", fail_after_file_unlink)
    with TestClient(
        _app(test_settings, session_factory, store), raise_server_exceptions=False
    ) as client:
        owner_id = _account(client, "media-delete-state-commit")
        video_id = _video(session_factory, store, owner_id)
        with session_factory() as session:
            storage_name = session.scalar(select(Video.storage_name).where(Video.id == video_id))
            upload_id = session.scalar(select(Video.upload_id).where(Video.id == video_id))
        assert storage_name is not None and upload_id is not None
        media_path = store.target_path(storage_name)
        media_path_holder.append(media_path)

        interrupted = client.delete(
            f"/videos/{video_id}/media",
            headers=mutate_headers(csrf(client)),
        )
        assert interrupted.status_code == 500
        assert not media_path.exists()
        with session_factory() as session:
            video = session.get(Video, video_id)
            intent = session.scalar(
                select(CleanupIntent).where(
                    CleanupIntent.upload_id == upload_id,
                    CleanupIntent.kind == "media",
                )
            )
            assert video is not None and video.media_status == "deletion_pending"
            assert intent is not None and intent.state == "pending"

        monkeypatch.setattr(video_services, "db_transaction", original_transaction)
        retried = client.post(
            f"/videos/{video_id}/media-deletion/retry",
            headers=mutate_headers(csrf(client)),
        )
        assert retried.status_code == 200
        assert retried.json()["media_status"] == "deleted"
        assert not media_path.exists()


def test_unconfirmed_execution_keeps_media_and_global_execution_occupancy(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    with TestClient(_app(test_settings, session_factory, store)) as client:
        owner_id = _account(client, "media-delete-running")
        video_id = _video(session_factory, store, owner_id)
        submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
        execution_id = uuid.uuid4()
        with session_factory.begin() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            attempt = session.get(AnalysisAttempt, submitted.attempt_id)
            assert task is not None and attempt is not None
            task.status = "running"
            attempt.status = "running"
            attempt.claim_count = 1
            attempt.execution_id = execution_id
            session.add(
                AnalysisExecution(
                    id=execution_id,
                    task_id=task.id,
                    attempt_id=attempt.id,
                    worker_id="still-running-test-worker",
                    backend_pid=2,
                )
            )
        with session_factory() as session:
            storage_name = session.get(Video, video_id).storage_name
        content_path = store.target_path(storage_name)
        assert content_path.exists()

        removed = client.delete(f"/videos/{video_id}/media", headers=mutate_headers(csrf(client)))
        assert removed.status_code == 202
        assert removed.json()["media_status"] == "deletion_pending"
        assert removed.json()["stop_confirmation_required"] is True
        assert content_path.exists()
        assert client.get(f"/videos/{video_id}/content").status_code == 410
        assert client.head(f"/videos/{video_id}/content").status_code == 410
        assert client.get(f"/videos/{video_id}/content", headers={"Range": "bytes=0-1"}).status_code == 410
        assert client.get(f"/videos/{video_id}/transcript").status_code == 200
        progress = client.get(f"/videos/{video_id}/media-deletion").json()
        assert progress["stop_confirmation_required"] is True
        with session_factory() as session:
            attempt = session.get(AnalysisAttempt, submitted.attempt_id)
            execution = session.get(AnalysisExecution, execution_id)
            intents = list(session.scalars(select(CleanupIntent).where(CleanupIntent.kind == "media")))
            assert attempt is not None and execution is not None
            assert attempt.status == "failed" and attempt.stop_confirmed_at is None
            assert execution.stop_confirmed_at is None
            assert len(intents) == 1 and intents[0].state == "pending"


def test_media_cleanup_failure_is_retryable_without_analysis_or_budget_changes(
    test_settings: Settings, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MediaStore(test_settings.app_data_root)
    original_remove = store.remove_media

    def fail_remove(_storage_name: str) -> None:
        raise OSError("injected media unlink failure")

    monkeypatch.setattr(store, "remove_media", fail_remove)
    with TestClient(_app(test_settings, session_factory, store)) as client:
        owner_id = _account(client, "media-delete-retry")
        video_id = _video(session_factory, store, owner_id)
        submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
        headers = mutate_headers(csrf(client))
        with session_factory() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            assert task is not None
            before = (
                task.auto_recovery_count,
                task.delivery_retry_count,
                task.delivery_operation_count,
            )

        failed = client.delete(f"/videos/{video_id}/media", headers=headers)
        assert failed.status_code == 202
        assert failed.json()["media_status"] == "cleanup_failed"
        assert failed.json()["error_code"] == "STORAGE_OPERATION_FAILED"
        assert client.get(f"/videos/{video_id}/content").status_code == 410
        assert client.head(f"/videos/{video_id}/content").status_code == 410
        assert client.get(
            f"/videos/{video_id}/content", headers={"Range": "bytes=0-1"}
        ).status_code == 410
        assert client.get(f"/videos/{video_id}/transcript").status_code == 200
        with session_factory() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            assert task is not None
            after_failure = (
                task.auto_recovery_count,
                task.delivery_retry_count,
                task.delivery_operation_count,
            )
            attempt_count = len(
                list(session.scalars(select(AnalysisAttempt).where(AnalysisAttempt.task_id == task.id)))
            )
            request_count = len(
                list(session.scalars(select(SummaryRequest).where(SummaryRequest.task_id == task.id)))
            )
        assert after_failure == before
        assert attempt_count == 1 and request_count == 0

        monkeypatch.setattr(store, "remove_media", original_remove)
        retried = client.post(f"/videos/{video_id}/media-deletion/retry", headers=headers)
        assert retried.status_code == 200
        assert retried.json()["media_status"] == "deleted"
        assert client.get(f"/videos/{video_id}/transcript").status_code == 200
        with session_factory() as session:
            attempts = list(
                session.scalars(select(AnalysisAttempt).where(AnalysisAttempt.task_id == submitted.task_id))
            )
            summaries = list(
                session.scalars(select(AnalysisSummary).where(AnalysisSummary.task_id == submitted.task_id))
            )
            task = session.get(AnalysisTask, submitted.task_id)
            assert task is not None
            assert len(attempts) == 1 and summaries == []
            assert (
                task.auto_recovery_count,
                task.delivery_retry_count,
                task.delivery_operation_count,
            ) == before


def test_whole_record_delete_can_finish_from_media_cleanup_failed(
    test_settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(test_settings.app_data_root)
    original_remove = store.remove_media

    def fail_remove(_storage_name: str) -> None:
        raise OSError("injected media unlink failure")

    monkeypatch.setattr(store, "remove_media", fail_remove)
    with TestClient(_app(test_settings, session_factory, store)) as client:
        owner_id = _account(client, "media-delete-whole-after-failure")
        video_id = _video(session_factory, store, owner_id)
        submitted = submit_analysis_task(session_factory, owner_id, video_id, test_settings)
        with session_factory() as session:
            upload_id = session.scalar(select(Video.upload_id).where(Video.id == video_id))
        assert upload_id is not None
        headers = mutate_headers(csrf(client))
        failed = client.delete(f"/videos/{video_id}/media", headers=headers)
        assert failed.status_code == 202
        assert failed.json()["media_status"] == "cleanup_failed"
        monkeypatch.setattr(store, "remove_media", original_remove)
        assert client.delete(f"/videos/{video_id}", headers=headers).status_code == 204
        assert client.get(f"/videos/{video_id}").status_code == 404
        assert client.get(f"/videos/{video_id}/transcript").status_code == 404
        with session_factory() as session:
            assert session.get(AnalysisTask, submitted.task_id) is None
            assert session.scalar(
                select(func.count(CleanupIntent.id)).where(CleanupIntent.upload_id == upload_id)
            ) == 0


def test_media_delete_enforces_csrf_origin_and_account_ownership(
    test_settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    store = MediaStore(test_settings.app_data_root)
    app = _app(test_settings, session_factory, store)
    with TestClient(app) as owner, TestClient(app) as stranger:
        owner_id = _account(owner, "media-delete-owner")
        video_id = _video(session_factory, store, owner_id)
        _account(stranger, "media-delete-stranger")
        assert owner.delete(f"/videos/{video_id}/media").status_code == 403
        token = csrf(owner)
        assert owner.delete(f"/videos/{video_id}/media", headers={"X-CSRF-Token": token}).status_code == 403
        stranger_headers = mutate_headers(csrf(stranger))
        assert stranger.delete(f"/videos/{video_id}/media", headers=stranger_headers).status_code == 404
        assert owner.get(f"/videos/{video_id}/content").status_code == 200
