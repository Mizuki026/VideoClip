from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from videoclip.analysis import submit_analysis_task
from videoclip.db import create_database_engine, create_session_factory
from videoclip.execution_gate import find_unconfirmed_execution
from videoclip.models import AnalysisAttempt, AnalysisExecution, AnalysisTask, UploadHandoff, Video
from videoclip.services import delete_video, delete_video_media, register_account, retry_video_media_cleanup
from videoclip.settings import Settings
from videoclip.stop_confirmation import (
    PROCESS_SCOPE,
    confirm_stopped_execution,
    identity_from_execution,
    worker_stop_evidence,
)
from videoclip.storage import MediaStore


def _proc_start_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    return int(fields[19])


def _session_exists(session_id: int) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = entry.joinpath("stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 3 and int(fields[3]) == session_id:
            return True
    return False


def _stop_child(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return not _session_exists(process.pid)


def main() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("real process acceptance must run inside the isolated Linux API container")
    container_id = os.environ["VIDEOCLIP_STAGE6_CONTAINER_ID"]
    settings = Settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    store = MediaStore(settings.app_data_root)
    store.prepare()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    video_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    try:
        if os.getsid(process.pid) != process.pid:
            raise AssertionError("the held process is not in its own Linux session")
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        started_at = datetime.now(UTC)
        owner = register_account(factory, f"stage6-process-{uuid.uuid4().hex[:12]}", uuid.uuid4().hex)
        owner_id = owner.id
        upload_id = uuid.uuid4()
        storage_name = uuid.uuid4().hex
        content = b"real-controlled-process-media-fixture"
        upload = UploadHandoff(
            id=upload_id,
            owner_id=owner_id,
            writer_id=uuid.uuid4(),
            part_name=f"{upload_id.hex}.part",
            target_name=storage_name,
            display_name="controlled-process.mp4",
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
            display_name=upload.display_name,
            storage_name=storage_name,
            byte_count=len(content),
            content_sha256=hashlib.sha256(content).hexdigest(),
            duration_seconds=30,
            media_info=upload.media_info,
            status="ready",
        )
        video_id = video.id
        with factory.begin() as session:
            session.add_all([upload, video])
        media_path = store.target_path(storage_name)
        media_path.write_bytes(content)
        submitted = submit_analysis_task(factory, owner_id, video_id, settings)
        execution_id = uuid.uuid4()
        with factory.begin() as session:
            task = session.get(AnalysisTask, submitted.task_id)
            attempt = session.get(AnalysisAttempt, submitted.attempt_id)
            assert task is not None and attempt is not None
            task.status = "running"
            task.stage = "transcribing"
            attempt.status = "running"
            attempt.claim_count = 1
            attempt.execution_id = execution_id
            attempt.first_claimed_at = started_at
            session.add(
                AnalysisExecution(
                    id=execution_id,
                    task_id=task.id,
                    attempt_id=attempt.id,
                    worker_id="stage6-real-process-acceptance",
                    backend_pid=1,
                    worker_instance_id=uuid.uuid4(),
                    worker_container_id=container_id,
                    process_scope=PROCESS_SCOPE,
                    boundary_pid=process.pid,
                    boundary_start_ticks=_proc_start_ticks(process.pid),
                    boundary_session_id=process.pid,
                    boundary_boot_id=boot_id,
                    supervisor_registered_at=started_at,
                )
            )

        completed = delete_video_media(factory, store, owner_id, video_id)
        with factory() as session:
            current = session.scalar(select(Video).where(Video.id == video_id))
            attempt = session.get(AnalysisAttempt, submitted.attempt_id)
            execution = session.get(AnalysisExecution, execution_id)
            blocking = find_unconfirmed_execution(session)
            identity = identity_from_execution(execution) if execution is not None else None
            assert current is not None and attempt is not None and execution is not None
            assert blocking is not None and blocking.id == execution_id
            assert current.media_status == "deletion_pending"
            assert attempt.stop_confirmed_at is None and execution.stop_confirmed_at is None
            assert identity is not None
            session.commit()
        if completed or not process_path_alive(process.pid) or not media_path.is_file():
            raise AssertionError("media cleanup crossed the live unconfirmed execution gate")

        session_stopped = _stop_child(process)
        if not session_stopped:
            raise AssertionError("controlled Linux process session did not stop and reap")
        evidence = worker_stop_evidence(identity, session_stopped=session_stopped)
        if confirm_stopped_execution(factory, evidence) != "confirmed":
            raise AssertionError("the exact stopped execution did not accept trusted evidence")
        if not retry_video_media_cleanup(factory, store, owner_id, video_id):
            raise AssertionError("media cleanup did not resume after trusted stop confirmation")
        if media_path.exists():
            raise AssertionError("media path remains after trusted stop and cleanup")
        if delete_video(factory, store, owner_id, video_id) != 204:
            raise AssertionError("whole-record deletion did not remove the isolated process fixture")
        print("held_live_execution=1 stop_confirmed_before_cleanup=1 media_deleted=1 whole_record_deleted=1")
    finally:
        if process.poll() is None:
            _stop_child(process)
        if video_id is not None and owner_id is not None:
            try:
                delete_video(factory, store, owner_id, video_id)
            except Exception:
                pass
        engine.dispose()


def process_path_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    main()
