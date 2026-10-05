from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from datetime import UTC, datetime

from videoclip.analysis import submit_analysis_task
from videoclip.db import create_database_engine, create_session_factory
from videoclip.models import (
    AnalysisAttempt,
    AnalysisExecution,
    AnalysisSummary,
    AnalysisTask,
    TranscriptSegment,
    UploadHandoff,
    Video,
)
from videoclip.services import register_account
from videoclip.settings import Settings
from videoclip.storage import MediaStore


def main() -> None:
    settings = Settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    username = os.environ["VIDEOCLIP_STAGE6_USERNAME"]
    password = os.environ["VIDEOCLIP_STAGE6_PASSWORD"]
    user = register_account(factory, username, password)

    upload_id = uuid.uuid4()
    storage_name = uuid.uuid4().hex
    store = MediaStore(settings.app_data_root)
    store.prepare()
    media_path = store.target_path(storage_name)
    subprocess.run(
        [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=steelblue:s=320x240:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "8",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-y",
            str(media_path),
        ],
        check=True,
        capture_output=True,
    )
    byte_count = media_path.stat().st_size
    digest = hashlib.sha256(media_path.read_bytes()).hexdigest()
    upload = UploadHandoff(
        id=upload_id,
        owner_id=user.id,
        writer_id=uuid.uuid4(),
        part_name=f"{upload_id.hex}.part",
        target_name=storage_name,
        display_name="stage6-synthetic-video.mp4",
        state="complete",
        byte_count=byte_count,
        content_sha256=digest,
        duration_seconds=8,
        media_info={
            "container": "mp4",
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "audio_codec": "aac",
            "timeline_start_seconds": 0,
            "streams": [
                {"index": 0, "type": "video", "codec": "h264", "start_time": 0, "time_base": "1/24"},
                {"index": 1, "type": "audio", "codec": "aac", "start_time": 0, "time_base": "1/16000"},
            ],
        },
    )
    video = Video(
        id=uuid.uuid4(),
        owner_id=user.id,
        upload_id=upload_id,
        display_name=upload.display_name,
        storage_name=storage_name,
        byte_count=byte_count,
        content_sha256=digest,
        duration_seconds=8,
        media_info=upload.media_info,
        status="ready",
    )
    with factory.begin() as session:
        session.add_all([upload, video])

    task_ref = submit_analysis_task(factory, user.id, video.id, settings)
    attempt_id = task_ref.attempt_id
    task_id = task_ref.task_id
    now = datetime.now(UTC)
    execution_id = uuid.uuid4()
    with factory.begin() as session:
        task = session.get(AnalysisTask, task_id)
        attempt = session.get(AnalysisAttempt, attempt_id)
        assert task is not None and attempt is not None
        task.status = "succeeded"
        task.stage = "succeeded"
        task.summary_status = "succeeded"
        attempt.status = "succeeded"
        attempt.claim_count = 1
        attempt.execution_id = execution_id
        attempt.first_claimed_at = now
        attempt.stop_confirmed_at = now
        attempt.manifest_fingerprint = "a" * 64
        session.add(
            AnalysisExecution(
                id=execution_id,
                task_id=task_id,
                attempt_id=attempt_id,
                worker_id="stage6-synthetic-fixture",
                backend_pid=1,
                worker_instance_id=uuid.uuid4(),
                worker_container_id="b" * 64,
                process_scope="linux_session_v1",
                boundary_pid=1,
                boundary_start_ticks=1,
                boundary_session_id=1,
                boundary_boot_id="stage6-fixture-boot",
                supervisor_registered_at=now,
                stop_confirmed_at=now,
                stop_confirmation_source="synthetic_acceptance_fixture",
                stop_reason="fixture_complete",
            )
        )
        session.add(
            TranscriptSegment(
                attempt_id=attempt_id,
                segment_number=0,
                audio_start_seconds=0,
                audio_end_seconds=8,
                actual_audio_seconds=8,
                video_start_seconds=0,
                video_end_seconds=8,
                audio_sha256="c" * 64,
                request_fingerprint="d" * 64,
                status="succeeded",
                text="合成测试引用保留",
                request_count=1,
            )
        )
        session.add(
            AnalysisSummary(
                task_id=task_id,
                video_id=video.id,
                attempt_id=attempt_id,
                manifest_fingerprint="a" * 64,
                transcript_fingerprint="e" * 64,
                structure_version="stage6-fixture-v1",
                provider="local-fixture",
                model="local-fixture",
                prompt_version="stage6-fixture-v1",
                prompt_sha256="f" * 64,
                response_schema_sha256="1" * 64,
                payload_sha256="2" * 64,
                payload={
                    "chapters": [
                        {
                            "title": "合成视频章节",
                            "evidence": [
                                {
                                    "segment_id": 0,
                                    "quote": "合成测试引用保留",
                                    "start_seconds": 0,
                                    "end_seconds": 8,
                                }
                            ],
                            "start_seconds": 0,
                            "end_seconds": 8,
                        }
                    ],
                    "key_points": [
                        {
                            "text": "删除原视频后笔记继续保留",
                            "evidence": [
                                {
                                    "segment_id": 0,
                                    "quote": "合成测试引用保留",
                                    "start_seconds": 0,
                                    "end_seconds": 8,
                                }
                            ],
                            "start_seconds": 0,
                            "end_seconds": 8,
                        }
                    ],
                },
            )
        )
    print(video.id)
    engine.dispose()


if __name__ == "__main__":
    main()
