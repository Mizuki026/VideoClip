from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, select

from videoclip.analysis_supervisor import (
    WorkerBoundary,
    establish_execution_boundary,
    register_execution_with_supervisor,
)
from videoclip.analysis_worker import ExecutionLease, _claim_attempt, _execution_identity
from videoclip.db import create_database_engine, create_session_factory
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
from videoclip.services import retry_attempt_cleanup
from videoclip.settings import get_settings
from videoclip.storage import MediaStore


def _fixture(
    session: Any, owner_id: uuid.UUID, *, suffix: str
) -> tuple[Video, AnalysisTask, AnalysisAttempt]:
    upload_id = uuid.uuid4()
    storage_name = uuid.uuid4().hex
    upload = UploadHandoff(
        id=upload_id,
        owner_id=owner_id,
        writer_id=uuid.uuid4(),
        part_name=f"{upload_id.hex}.part",
        target_name=storage_name,
        display_name=f"stop-confirmation-{suffix}.mp4",
        state="complete",
        byte_count=1,
        content_sha256=hashlib.sha256(b"x").hexdigest(),
        duration_seconds=1.0,
        media_info={"container": "mp4", "timeline_start_seconds": 0.0, "streams": []},
    )
    session.add(upload)
    session.flush()
    video = Video(
        id=uuid.uuid4(),
        owner_id=owner_id,
        upload_id=upload_id,
        display_name=upload.display_name,
        storage_name=storage_name,
        byte_count=1,
        content_sha256=upload.content_sha256,
        duration_seconds=1.0,
        media_info=upload.media_info,
        status="ready",
    )
    session.add(video)
    session.flush()
    task = AnalysisTask(
        id=uuid.uuid4(),
        video_id=video.id,
        owner_id=owner_id,
        status="queued",
        stage="queued",
        summary_status="not_started",
    )
    session.add(task)
    session.flush()
    attempt = AnalysisAttempt(
        id=uuid.uuid4(),
        task_id=task.id,
        attempt_number=1,
        status="queued",
        execution_mode="full",
        config_snapshot={"acceptance_fixture": True},
        config_sha256="a" * 64,
        source_fingerprint="b" * 64,
        work_directory_name=uuid.uuid4().hex,
        claim_count=0,
        cleanup_state="pending",
    )
    session.add(attempt)
    session.flush()
    task.current_attempt_id = attempt.id
    video.current_analysis_task_id = task.id
    session.add(TaskDelivery(attempt_id=attempt.id, status="pending"))
    return video, task, attempt


def _setup_rows(
    factory: Any,
) -> tuple[uuid.UUID, Video, AnalysisTask, AnalysisAttempt, AnalysisTask, AnalysisAttempt]:
    owner_id = uuid.uuid4()
    with factory.begin() as session:
        if session.scalar(
            select(AnalysisExecution.id).where(AnalysisExecution.stop_confirmed_at.is_(None)).limit(1)
        ) is not None:
            raise RuntimeError(
                "an existing unconfirmed Stage 2 execution blocks the isolated acceptance fixture"
            )
        owner = User(username=f"stage3-{owner_id.hex[:20]}", password_hash="isolated-stop-proof-fixture")
        session.add(owner)
        session.flush()
        active_video, active_task, active_attempt = _fixture(session, owner.id, suffix="active")
        _, queued_task, queued_attempt = _fixture(session, owner.id, suffix="queued")
        return owner.id, active_video, active_task, active_attempt, queued_task, queued_attempt


def _model_stub_server_code() -> str:
    return """
import signal, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
signal.signal(signal.SIGTERM, signal.SIG_IGN)
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        time.sleep(600)
    def log_message(self, *_args):
        pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""


def run_fixture() -> int:
    if os.environ.get("ANALYSIS_STOP_ACCEPTANCE") != "1":
        raise RuntimeError("set ANALYSIS_STOP_ACCEPTANCE=1 only for the isolated Stage 3 fault run")
    settings = get_settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    lease: ExecutionLease | None = None
    children: list[subprocess.Popen[bytes]] = []
    try:
        boundary: WorkerBoundary = establish_execution_boundary()
        owner_id, video, task, attempt, queued_task, queued_attempt = _setup_rows(factory)
        lease = ExecutionLease.acquire(engine)
        if lease is None:
            raise RuntimeError("the global analysis advisory lock was unavailable")
        claim = _claim_attempt(
            lease,
            task.id,
            attempt.id,
            f"stage3-fault-{os.getpid()}",
            boundary,
        )
        if claim is None:
            raise RuntimeError("the isolated active attempt could not be claimed")
        identity = _execution_identity(claim)
        register_execution_with_supervisor(identity)

        cleanup_sentinel: Path | None = None
        if os.environ.get("ANALYSIS_STOP_ACCEPTANCE_CLEANUP_SENTINEL") == "1":
            sentinel_path = (
                MediaStore(settings.app_data_root).analysis
                / task.id.hex
                / attempt.work_directory_name
                / "stop-proof-sentinel"
            )
            sentinel_path.parent.mkdir(parents=True, exist_ok=True)
            sentinel_path.write_text("keep until stop evidence", encoding="utf-8")
            cleanup_sentinel = sentinel_path

        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = int(reservation.getsockname()[1])
        process_directory = Path(tempfile.mkdtemp(prefix="videoclip-stage3-processes-"))
        audio_path = process_directory / "isolated.wav"
        audio_path.write_bytes(b"isolated-fault-acceptance-audio")
        asr_output_path = process_directory / "asr-response.json"
        llm_request_path = process_directory / "llm-request.json"
        llm_output_path = process_directory / "llm-response.json"
        llm_request_path.write_text(
            json.dumps(
                {
                    "messages": [
                        {"role": "system", "content": "isolated fault test"},
                        {"role": "user", "content": "return a local fixture"},
                    ],
                    "timeouts": {
                        "total": 600,
                        "connect": 2,
                        "read": 600,
                        "write": 600,
                        "pool": 2,
                    },
                    "max_response_bytes": 4096,
                    "model": "local-stage3-fixture",
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "max_output_tokens": 8,
                    "temperature": 0,
                    "enable_thinking": False,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        children.append(
            subprocess.Popen(
                [sys.executable, "-c", _model_stub_server_code(), str(port)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                process_group=0,
            )
        )
        children.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "videoclip.asr_client",
                    "--audio",
                    str(audio_path),
                    "--output",
                    str(asr_output_path),
                    "--base-url",
                    f"http://127.0.0.1:{port}/v1",
                    "--model",
                    "local-stage3-fixture",
                    "--total-timeout",
                    "600",
                    "--connect-timeout",
                    "2",
                    "--read-timeout",
                    "600",
                    "--maximum-response-bytes",
                    "4096",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "SILICONFLOW_API_KEY": "stage3-local-placeholder"},
                process_group=0,
            )
        )
        children.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "videoclip.llm_client",
                    "--request",
                    str(llm_request_path),
                    "--output",
                    str(llm_output_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "SILICONFLOW_API_KEY": "stage3-local-placeholder"},
                process_group=0,
            )
        )
        children.append(
            subprocess.Popen(
                [
                    "/usr/bin/ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-re",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=32x32:rate=1",
                    "-f",
                    "null",
                    "-",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                process_group=0,
            )
        )
        time.sleep(0.5)
        if any(child.poll() is not None for child in children):
            raise RuntimeError("an acceptance subprocess exited before the parent was terminated")
        ready = {
            "event": "ready",
            "owner_id": str(owner_id),
            "task_id": str(task.id),
            "video_id": str(video.id),
            "attempt_id": str(attempt.id),
            "execution_id": str(claim.execution_id),
            "queued_task_id": str(queued_task.id),
            "queued_attempt_id": str(queued_attempt.id),
            "worker_instance_id": str(boundary.worker_instance_id),
            "worker_container_id": boundary.worker_container_id,
            "boundary_pid": boundary.boundary_pid,
            "boundary_start_ticks": boundary.boundary_start_ticks,
            "boundary_session_id": boundary.boundary_session_id,
            "database_backend_pid": lease.backend_pid,
            "process_directory": str(process_directory),
            "cleanup_sentinel": str(cleanup_sentinel) if cleanup_sentinel is not None else None,
            "children": [
                {"role": role, "pid": child.pid}
                for role, child in zip(
                    ("local_model_stub", "asr_request", "llm_request", "ffmpeg_media"),
                    children,
                    strict=True,
                )
            ],
        }
        print(json.dumps(ready, separators=(",", ":")), flush=True)
        threading_event = threading.Event()
        while not threading_event.wait(60):
            pass
        return 0
    finally:
        if lease is not None:
            lease.release()
        engine.dispose()


def probe_claim(task_id: uuid.UUID, attempt_id: uuid.UUID) -> int:
    settings = get_settings()
    engine = create_database_engine(settings)
    try:
        lease = ExecutionLease.acquire(engine)
        if lease is None:
            print(json.dumps({"claim": "blocked_by_advisory_lock"}))
            return 0
        try:
            claim = _claim_attempt(lease, task_id, attempt_id, "stage3-claim-probe")
            if claim is not None:
                raise RuntimeError("an unconfirmed execution did not block a second claim")
            print(json.dumps({"claim": "blocked_by_unconfirmed_execution"}))
            return 0
        finally:
            lease.release()
    finally:
        engine.dispose()


def probe_attempt_cleanup(owner_id: uuid.UUID, video_id: uuid.UUID, attempt_id: uuid.UUID) -> int:
    settings = get_settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            video = session.get(Video, video_id)
            task = session.get(AnalysisTask, video.current_analysis_task_id) if video is not None else None
            attempt = session.get(AnalysisAttempt, attempt_id)
            if (
                video is None
                or video.owner_id != owner_id
                or task is None
                or task.owner_id != owner_id
                or attempt is None
                or attempt.task_id != task.id
            ):
                raise RuntimeError("cleanup probe identity does not match the isolated fixture")
            sentinel = (
                MediaStore(settings.app_data_root).analysis
                / task.id.hex
                / attempt.work_directory_name
                / "stop-proof-sentinel"
            )
        try:
            retry_attempt_cleanup(
                factory, MediaStore(settings.app_data_root), owner_id, video_id, attempt_id
            )
        except HTTPException as exc:
            detail = exc.detail
            if not isinstance(detail, dict) or detail.get("code") != "STOP_CONFIRMATION_REQUIRED":
                raise RuntimeError("attempt cleanup returned an unexpected pre-stop response") from exc
            if detail.get("cleanup_eligible") is not False or not sentinel.is_file():
                raise RuntimeError("attempt cleanup was not safely blocked before stop proof") from exc
            print(json.dumps({"cleanup": "blocked_by_stop_confirmation", "sentinel_preserved": True}))
            return 0
        raise RuntimeError("attempt cleanup did not reject an unconfirmed execution")
    finally:
        engine.dispose()


def cleanup_fixtures(
    owner_id: uuid.UUID,
    task_ids: list[uuid.UUID],
    attempt_ids: list[uuid.UUID],
    execution_ids: list[uuid.UUID],
) -> int:
    settings = get_settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            owner = session.get(User, owner_id, with_for_update=True)
            if owner is None or not owner.username.startswith("stage3-"):
                raise RuntimeError("refusing to clean a non-acceptance account")
            executions = list(
                session.scalars(
                    select(AnalysisExecution).where(AnalysisExecution.id.in_(execution_ids))
                )
            )
            owned_task_ids = set(
                session.scalars(select(AnalysisTask.id).where(AnalysisTask.owner_id == owner_id))
            )
            owned_attempt_ids = set(
                session.scalars(
                    select(AnalysisAttempt.id)
                    .join(AnalysisTask, AnalysisTask.id == AnalysisAttempt.task_id)
                    .where(AnalysisTask.owner_id == owner_id)
                )
            )
            task_execution_ids = set(
                session.scalars(
                    select(AnalysisExecution.id).where(AnalysisExecution.task_id.in_(task_ids))
                )
            )
            if owned_task_ids != set(task_ids) or owned_attempt_ids != set(attempt_ids):
                raise RuntimeError("refusing to clean rows that do not exactly belong to the fixture account")
            if (
                task_execution_ids != set(execution_ids)
                or {item.id for item in executions} != set(execution_ids)
            ):
                raise RuntimeError("refusing to clean a fixture with an unlisted or missing execution")
            if any(item.stop_confirmed_at is None for item in executions):
                raise RuntimeError("refusing to remove an unconfirmed execution fixture")
            if any(
                item.task_id not in set(task_ids) or item.attempt_id not in set(attempt_ids)
                for item in executions
            ):
                raise RuntimeError("refusing to clean an execution outside the fixture identity set")
            session.execute(delete(SummaryRequest).where(SummaryRequest.attempt_id.in_(attempt_ids)))
            session.execute(delete(AnalysisSummary).where(AnalysisSummary.task_id.in_(task_ids)))
            session.execute(delete(TranscriptSegment).where(TranscriptSegment.attempt_id.in_(attempt_ids)))
            session.execute(delete(TaskDelivery).where(TaskDelivery.attempt_id.in_(attempt_ids)))
            session.execute(delete(AnalysisExecution).where(AnalysisExecution.id.in_(execution_ids)))
            tasks = list(session.scalars(select(AnalysisTask).where(AnalysisTask.id.in_(task_ids))))
            videos = list(
                session.scalars(
                    select(Video).where(
                        Video.owner_id == owner_id,
                        Video.current_analysis_task_id.in_(task_ids),
                    )
                )
            )
            if len(videos) != 2:
                raise RuntimeError("refusing to clean a fixture with an unexpected video count")
            for task in tasks:
                task.current_attempt_id = None
            for video in videos:
                video.current_analysis_task_id = None
            session.flush()
            session.execute(delete(AnalysisAttempt).where(AnalysisAttempt.id.in_(attempt_ids)))
            session.execute(delete(AnalysisTask).where(AnalysisTask.id.in_(task_ids)))
            upload_ids = [video.upload_id for video in videos]
            session.execute(delete(Video).where(Video.id.in_([video.id for video in videos])))
            session.execute(delete(CleanupIntent).where(CleanupIntent.owner_id == owner_id))
            session.execute(delete(UploadHandoff).where(UploadHandoff.id.in_(upload_ids)))
            session.delete(owner)
        print(json.dumps({"fixtures_removed": True, "task_count": len(task_ids)}))
        return 0
    finally:
        engine.dispose()


def main() -> int:
    if os.environ.get("ANALYSIS_STOP_ACCEPTANCE") != "1":
        raise RuntimeError("set ANALYSIS_STOP_ACCEPTANCE=1 only for the isolated Stage 3 fault run")
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("run")
    probe = subparsers.add_parser("probe")
    probe.add_argument("--task-id", required=True, type=uuid.UUID)
    probe.add_argument("--attempt-id", required=True, type=uuid.UUID)
    cleanup_probe = subparsers.add_parser("cleanup-probe")
    cleanup_probe.add_argument("--owner-id", required=True, type=uuid.UUID)
    cleanup_probe.add_argument("--video-id", required=True, type=uuid.UUID)
    cleanup_probe.add_argument("--attempt-id", required=True, type=uuid.UUID)
    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--owner-id", required=True, type=uuid.UUID)
    cleanup.add_argument("--task-id", required=True, action="append", type=uuid.UUID)
    cleanup.add_argument("--attempt-id", required=True, action="append", type=uuid.UUID)
    cleanup.add_argument("--execution-id", required=True, action="append", type=uuid.UUID)
    args = parser.parse_args()
    if args.operation == "run":
        return run_fixture()
    if args.operation == "probe":
        return probe_claim(args.task_id, args.attempt_id)
    if args.operation == "cleanup-probe":
        return probe_attempt_cleanup(args.owner_id, args.video_id, args.attempt_id)
    return cleanup_fixtures(args.owner_id, args.task_id, args.attempt_id, args.execution_id)


if __name__ == "__main__":
    raise SystemExit(main())
