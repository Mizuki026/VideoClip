from __future__ import annotations

# 本模块执行已领取的分析尝试：在受控执行边界内生成转写检查点，并按冻结配置校验和发布摘要。

import hashlib
import json
import logging
import math
import os
import random
import signal
import socket
import subprocess
import sys
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from videoclip.analysis import ASR_REQUEST_FINGERPRINT_VERSION, canonical_sha256
from videoclip.analysis_supervisor import (
    BoundaryUnknown,
    WorkerBoundary,
    establish_execution_boundary,
    register_execution_with_supervisor,
)
from videoclip.analysis_wait import enter_model_wait, require_manual_wait, wait_budget_error
from videoclip.db import create_database_engine
from videoclip.events import record_analysis_event
from videoclip.execution_gate import (
    EXECUTION_ADVISORY_LOCK_KEY,
    find_next_queued_task,
    find_unconfirmed_execution,
)
from videoclip.model_cooling import (
    ASR_BACKPRESSURE_ERROR_CODES,
    LLM_BACKPRESSURE_ERROR_CODES,
    MODEL_BACKPRESSURE_ERROR_CODES,
    CooldownPolicy,
    ModelIdentity,
    current_cooldown_until,
    extend_shared_cooldown,
    model_identity_from_config,
    record_model_success,
)
from videoclip.models import (
    AnalysisAttempt,
    AnalysisExecution,
    AnalysisSummary,
    AnalysisTask,
    SummaryRequest,
    TranscriptSegment,
    Video,
)
from videoclip.recovery_rules import checkpoint_matches
from videoclip.services import db_transaction
from videoclip.settings import Settings, get_settings
from videoclip.stop_confirmation import ExecutionIdentity
from videoclip.storage import MediaStore
from videoclip.summarization import (
    SummaryFailure,
    build_summary_prompt,
    correction_for_failure,
    is_repairable_summary_failure,
    resolve_summary_contract,
    summary_request_fingerprint,
    validate_context_budget,
    validate_summary_output,
    validate_transcript_context_budget,
    validate_transcript_manifest,
)
from videoclip.summarization import (
    canonical_sha256 as summary_canonical_sha256,
)

logger = logging.getLogger("videoclip.analysis_worker")
ADVISORY_LOCK_KEY = EXECUTION_ADVISORY_LOCK_KEY
PCM_SAMPLE_WIDTH = 2
CHECK_INTERVAL_SECONDS = 1.0


class AnalysisFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retry_after: str | None = None,
        response_received_at: datetime | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.response_received_at = response_received_at


class ModelCooldownWait(RuntimeError):
    """The persisted preflight gate blocked a call before request reservation."""


def _cooldown_policy(settings: Settings) -> CooldownPolicy:
    return CooldownPolicy(
        backoff_base_seconds=settings.model_cooldown_backoff_base_seconds,
        backoff_max_seconds=settings.model_cooldown_backoff_max_seconds,
    )


def _response_received_at(payload: dict[str, Any]) -> datetime | None:
    value = payload.get("response_received_at")
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _backpressure_identity(claim: ClaimedAttempt, code: str, settings: Settings) -> ModelIdentity:
    field = "asr" if code.startswith("ASR_") else "summary"
    section = claim.snapshot.get(field)
    if not isinstance(section, dict):
        raise AnalysisFailure("MODEL_COOLDOWN_IDENTITY_INVALID")
    return model_identity_from_config(
        section,
        default_account_reference=settings.siliconflow_account_reference,
    )


class StopUnconfirmed(AnalysisFailure):
    def __init__(self) -> None:
        super().__init__("PROCESS_STOP_UNCONFIRMED")


@dataclass(frozen=True)
class ClaimedAttempt:
    task_id: uuid.UUID
    attempt_id: uuid.UUID
    parent_attempt_id: uuid.UUID | None
    execution_id: uuid.UUID
    video_id: uuid.UUID
    storage_name: str
    snapshot: dict[str, Any]
    work_directory_name: str
    execution_mode: str = "full"
    worker_instance_id: uuid.UUID | None = None
    worker_container_id: str | None = None
    process_scope: str | None = None
    boundary_pid: int | None = None
    boundary_start_ticks: int | None = None
    boundary_session_id: int | None = None
    boundary_boot_id: str | None = None


@dataclass(frozen=True)
class ManifestEntry:
    segment_number: int
    audio_start_seconds: float
    audio_end_seconds: float
    actual_audio_seconds: float
    video_start_seconds: float
    video_end_seconds: float
    pcm_start_byte: int
    pcm_end_byte: int


# 持有数据库专用连接和会话级互斥锁，整个分析执行期间占用唯一执行位。
class ExecutionLease:
    """Dedicated PostgreSQL session retaining the deployment-wide advisory lock."""

    def __init__(self, connection: Connection, backend_pid: int) -> None:
        self.connection = connection
        self.backend_pid = backend_pid
        self.acquired = True

    @classmethod
    def acquire(cls, engine: Engine) -> ExecutionLease | None:
        connection = engine.connect()
        try:
            locked = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": ADVISORY_LOCK_KEY}
                ).scalar_one()
            )
            connection.commit()
            if not locked:
                connection.close()
                return None
            row = connection.execute(
                text(
                    "SELECT pg_backend_pid(), EXISTS ("
                    "SELECT 1 FROM pg_locks "
                    "WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted "
                    "AND classid::bigint = ((:lock_key >> 32) & 4294967295) "
                    "AND objid::bigint = (:lock_key & 4294967295) AND objsubid = 1"
                    ")"
                ),
                {"lock_key": ADVISORY_LOCK_KEY},
            ).one()
            connection.commit()
            if not row[1]:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": ADVISORY_LOCK_KEY}
                )
                connection.commit()
                connection.close()
                return None
            return cls(connection, int(row[0]))
        except BaseException:
            try:
                connection.invalidate()
            except SQLAlchemyError:
                pass
            connection.close()
            raise

    def assert_alive(self) -> None:
        try:
            row = self.connection.execute(
                text(
                    "SELECT pg_backend_pid(), EXISTS ("
                    "SELECT 1 FROM pg_locks "
                    "WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted "
                    "AND classid::bigint = ((:lock_key >> 32) & 4294967295) "
                    "AND objid::bigint = (:lock_key & 4294967295) AND objsubid = 1"
                    ")"
                ),
                {"lock_key": ADVISORY_LOCK_KEY},
            ).one()
            self.connection.commit()
        except SQLAlchemyError as exc:
            raise AnalysisFailure("EXECUTION_DATABASE_LOCK_LOST") from exc
        if int(row[0]) != self.backend_pid or not row[1]:
            raise AnalysisFailure("EXECUTION_DATABASE_LOCK_LOST")

    def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False
        try:
            if not self.connection.closed:
                self.connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": ADVISORY_LOCK_KEY}
                )
                self.connection.commit()
        except SQLAlchemyError:
            try:
                self.connection.invalidate()
            except SQLAlchemyError:
                pass
        finally:
            self.connection.close()


# 从已探测媒体生成不可变片段清单，记录音频区间、视频映射和源文件指纹。
def build_manifest(
    *, pcm_bytes: int, duration_seconds: float, sample_rate: int, channels: int, segment_seconds: int
) -> list[ManifestEntry]:
    frame_width = PCM_SAMPLE_WIDTH * channels
    if pcm_bytes <= 0 or pcm_bytes % frame_width != 0:
        raise AnalysisFailure("EXTRACTED_AUDIO_INVALID")
    total_frames = pcm_bytes // frame_width
    audio_duration = total_frames / sample_rate
    if audio_duration <= 0 or abs(audio_duration - duration_seconds) > 1.0:
        raise AnalysisFailure("AUDIO_DURATION_MISMATCH")
    # The verified media allowlist has zero track offsets. Clip any codec padding to the
    # already-probed original-video duration before mapping segment ends to playback time.
    total_frames = min(total_frames, int(duration_seconds * sample_rate))
    frames_per_segment = segment_seconds * sample_rate
    entries: list[ManifestEntry] = []
    for number, first_frame in enumerate(range(0, total_frames, frames_per_segment)):
        final_frame = min(first_frame + frames_per_segment, total_frames)
        start = first_frame / sample_rate
        end = final_frame / sample_rate
        if end <= start:
            raise AnalysisFailure("EXTRACTED_AUDIO_INVALID")
        entries.append(
            ManifestEntry(
                segment_number=number,
                audio_start_seconds=round(start, 6),
                audio_end_seconds=round(end, 6),
                actual_audio_seconds=round((final_frame - first_frame) / sample_rate, 6),
                video_start_seconds=round(start, 6),
                video_end_seconds=round(end, 6),
                pcm_start_byte=first_frame * frame_width,
                pcm_end_byte=final_frame * frame_width,
            )
        )
    return entries


def _lock_execution_rows(
    session: Session,
    claim: ClaimedAttempt,
    *,
    allow_invalid_video: bool = False,
) -> tuple[AnalysisTask, AnalysisAttempt, Video, AnalysisExecution]:
    video = session.scalar(select(Video).where(Video.id == claim.video_id).with_for_update())
    task = session.scalar(select(AnalysisTask).where(AnalysisTask.id == claim.task_id).with_for_update())
    attempt = session.scalar(
        select(AnalysisAttempt).where(AnalysisAttempt.id == claim.attempt_id).with_for_update()
    )
    execution = session.scalar(
        select(AnalysisExecution).where(AnalysisExecution.id == claim.execution_id).with_for_update()
    )
    if (
        video is None
        or task is None
        or attempt is None
        or execution is None
        or task.video_id != video.id
        or task.current_attempt_id != attempt.id
        or video.current_analysis_task_id != task.id
        or attempt.task_id != task.id
        or attempt.execution_id != execution.id
        or execution.attempt_id != attempt.id
        or execution.task_id != task.id
        or execution.stop_confirmed_at is not None
        or (
            claim.worker_instance_id is not None
            and (
                execution.worker_instance_id != claim.worker_instance_id
                or execution.worker_container_id != claim.worker_container_id
                or execution.process_scope != claim.process_scope
                or execution.boundary_pid != claim.boundary_pid
                or execution.boundary_start_ticks != claim.boundary_start_ticks
                or execution.boundary_session_id != claim.boundary_session_id
                or execution.boundary_boot_id != claim.boundary_boot_id
            )
        )
        or attempt.claim_count != 1
        or (
            not allow_invalid_video
            and (video.status != "ready" or video.media_status != "available")
        )
    ):
        raise AnalysisFailure("EXECUTION_OWNERSHIP_LOST" if video is not None else "VIDEO_UNAVAILABLE")
    return task, attempt, video, execution


# 收到消息后重新检查当前任务、排队顺序和全局执行位，避免重复或越权领取。
def _claim_attempt(
    lease: ExecutionLease,
    task_id: uuid.UUID,
    attempt_id: uuid.UUID,
    worker_id: str,
    boundary: WorkerBoundary | None = None,
) -> ClaimedAttempt | None:
    maker = sessionmaker(bind=lease.connection, autoflush=False, expire_on_commit=False)
    with db_transaction(maker) as session:
        candidate = session.get(AnalysisTask, task_id)
        if candidate is None:
            return None
        video = session.scalar(select(Video).where(Video.id == candidate.video_id).with_for_update())
        task = session.scalar(select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update())
        if task is None or task.current_attempt_id != attempt_id:
            return None
        attempt = session.scalar(
            select(AnalysisAttempt).where(AnalysisAttempt.id == attempt_id).with_for_update()
        )
        if (
            attempt is None
            or video is None
            or task.status != "queued"
            or attempt.status != "queued"
            or attempt.claim_count != 0
            or video.status != "ready"
            or video.media_status != "available"
            or video.current_analysis_task_id != task.id
            or attempt.task_id != task.id
        ):
            return None
        # 收到消息不代表可以执行；领取阶段还要核对全局未确认执行位。
        if find_unconfirmed_execution(session) is not None:
            return None
        if find_next_queued_task(session) != task.id:
            return None
        execution_id = uuid.uuid4()
        execution = AnalysisExecution(
            id=execution_id,
            task_id=task.id,
            attempt_id=attempt.id,
            worker_id=worker_id[:128],
            backend_pid=lease.backend_pid,
            worker_instance_id=boundary.worker_instance_id if boundary is not None else None,
            worker_container_id=boundary.worker_container_id if boundary is not None else None,
            process_scope=boundary.process_scope if boundary is not None else None,
            boundary_pid=boundary.boundary_pid if boundary is not None else None,
            boundary_start_ticks=boundary.boundary_start_ticks if boundary is not None else None,
            boundary_session_id=boundary.boundary_session_id if boundary is not None else None,
            boundary_boot_id=boundary.boundary_boot_id if boundary is not None else None,
        )
        session.add(execution)
        attempt.claim_count = 1
        attempt.execution_id = execution_id
        now = datetime.now(UTC)
        attempt.first_claimed_at = now
        attempt.status = "running"
        task.status = "running"
        task.delivery_next_check_at = None
        task.delivery_wait_reason = None
        if task.delivery_deadline_at is not None:
            task.delivery_blocked_since = now
        task.stage = "summarizing" if attempt.execution_mode == "summary_only" else "validating_source"
        if attempt.execution_mode == "summary_only":
            task.summary_status = "running"
        session.flush()
        record_analysis_event(
            session,
            task_id=task.id,
            event_key=f"execution-claimed:{execution_id}",
            event_type="execution_claimed",
            attempt_id=attempt.id,
            execution_id=execution_id,
            stage=task.stage,
        )
        return ClaimedAttempt(
            task_id=task.id,
            attempt_id=attempt.id,
            parent_attempt_id=attempt.parent_attempt_id,
            execution_id=execution_id,
            video_id=video.id,
            storage_name=video.storage_name,
            snapshot=attempt.config_snapshot,
            work_directory_name=attempt.work_directory_name,
            execution_mode=attempt.execution_mode,
            worker_instance_id=execution.worker_instance_id,
            worker_container_id=execution.worker_container_id,
            process_scope=execution.process_scope,
            boundary_pid=execution.boundary_pid,
            boundary_start_ticks=execution.boundary_start_ticks,
            boundary_session_id=execution.boundary_session_id,
            boundary_boot_id=execution.boundary_boot_id,
        )


def _execution_identity(claim: ClaimedAttempt) -> ExecutionIdentity:
    if (
        claim.worker_instance_id is None
        or claim.worker_container_id is None
        or claim.process_scope is None
        or claim.boundary_pid is None
        or claim.boundary_start_ticks is None
        or claim.boundary_session_id is None
        or claim.boundary_boot_id is None
    ):
        raise BoundaryUnknown("the claimed execution has no complete supervised process binding")
    return ExecutionIdentity(
        execution_id=claim.execution_id,
        task_id=claim.task_id,
        attempt_id=claim.attempt_id,
        worker_instance_id=claim.worker_instance_id,
        worker_container_id=claim.worker_container_id,
        process_scope=claim.process_scope,
        boundary_pid=claim.boundary_pid,
        boundary_start_ticks=claim.boundary_start_ticks,
        boundary_session_id=claim.boundary_session_id,
        boundary_boot_id=claim.boundary_boot_id,
    )


def _validate_execution(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    *,
    allow_invalid_video: bool = False,
) -> None:
    lease.assert_alive()
    with maker() as session:
        _lock_execution_rows(session, claim, allow_invalid_video=allow_invalid_video)
        session.commit()
    lease.assert_alive()


def _set_stage(
    maker: sessionmaker[Session], lease: ExecutionLease, claim: ClaimedAttempt, stage: str
) -> None:
    lease.assert_alive()
    with db_transaction(maker) as session:
        task, attempt, _, _ = _lock_execution_rows(session, claim)
        task.stage = stage
        if stage == "summarizing":
            task.status = "running"
            task.summary_status = "running"
            attempt.status = "running"


def _progress_check(maker: sessionmaker[Session], lease: ExecutionLease, claim: ClaimedAttempt) -> None:
    _validate_execution(maker, lease, claim)


def _signal_number(signal_name: str) -> int:
    value = getattr(signal, signal_name, None)
    if not isinstance(value, int):
        raise StopUnconfirmed()
    return value


def _signal_process_group(process_group: int, signal_number: int) -> None:
    kill_group = getattr(os, "killpg", None)
    if kill_group is None:
        raise StopUnconfirmed()
    kill_group(process_group, signal_number)


# 先请求子进程组退出，再按期限升级信号；未确认整组退出前不释放执行身份。
def _stop_process_group(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    process_group = process.pid
    if os.name == "nt":
        if process.poll() is None:
            process.terminate()
    else:
        try:
            _signal_process_group(process_group, _signal_number("SIGTERM"))
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                _signal_process_group(process_group, _signal_number("SIGKILL"))
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired as exc:
            raise StopUnconfirmed() from exc
    if os.name != "nt":
        end = time.monotonic() + grace_seconds
        while time.monotonic() < end:
            try:
                _signal_process_group(process_group, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        try:
            _signal_process_group(process_group, _signal_number("SIGKILL"))
        except ProcessLookupError:
            return
        end = time.monotonic() + grace_seconds
        while time.monotonic() < end:
            try:
                _signal_process_group(process_group, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        raise StopUnconfirmed()


# 在监督边界内启动外部程序，并把退出码、超时和停止证据交回调用方。
def _run_process(
    command: list[str],
    *,
    timeout_seconds: float,
    grace_seconds: float,
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    attempt_deadline: float,
    timeout_error_code: str = "LOCAL_PROCESS_TIMEOUT",
) -> None:
    if timeout_seconds <= 0:
        raise AnalysisFailure("ATTEMPT_TIMEOUT")
    launch_options: dict[str, Any] = {}
    if os.name == "nt":
        launch_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # A fresh process group stays inside the execution's supervised Linux session.
        launch_options["process_group"] = 0
    try:
        with db_transaction(maker) as session:
            _lock_execution_rows(session, claim)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **launch_options,
            )
    except OSError as exc:
        raise AnalysisFailure("LOCAL_PROCESS_START_FAILED") from exc
    started = time.monotonic()
    next_check = started
    try:
        while True:
            return_code = process.poll()
            if return_code is not None:
                break
            now = time.monotonic()
            if now >= attempt_deadline:
                _stop_process_group(process, grace_seconds)
                raise AnalysisFailure("ATTEMPT_TIMEOUT")
            if now - started >= timeout_seconds:
                _stop_process_group(process, grace_seconds)
                raise AnalysisFailure(timeout_error_code)
            if now >= next_check:
                _progress_check(maker, lease, claim)
                next_check = now + CHECK_INTERVAL_SECONDS
            time.sleep(0.1)
        if os.name != "nt":
            _wait_group_exit(process.pid, grace_seconds)
        if return_code != 0:
            raise AnalysisFailure("LOCAL_PROCESS_FAILED")
    except BaseException as exc:
        if process.poll() is None:
            try:
                _stop_process_group(process, grace_seconds)
            except StopUnconfirmed:
                raise
        if isinstance(exc, AnalysisFailure | KeyboardInterrupt | SystemExit):
            raise
        raise AnalysisFailure("LOCAL_PROCESS_CONTROL_FAILED") from exc


def _wait_group_exit(process_group: int, grace_seconds: float) -> None:
    if os.name == "nt":
        return
    end = time.monotonic() + grace_seconds
    while time.monotonic() < end:
        try:
            _signal_process_group(process_group, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        _signal_process_group(process_group, _signal_number("SIGKILL"))
    except ProcessLookupError:
        return
    end = time.monotonic() + grace_seconds
    while time.monotonic() < end:
        try:
            _signal_process_group(process_group, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise StopUnconfirmed()


def _hash_source(
    path: Path,
    expected_bytes: int,
    expected_sha256: str,
    *,
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    attempt_deadline: float,
) -> None:
    _progress_check(maker, lease, claim)
    digest = hashlib.sha256()
    last_check = time.monotonic()
    try:
        with db_transaction(maker) as session:
            _lock_execution_rows(session, claim)
            if path.is_symlink() or not path.is_file() or path.stat().st_size != expected_bytes:
                raise AnalysisFailure("VIDEO_SOURCE_CHANGED")
            source = path.open("rb")
        with source:
            while block := source.read(1024 * 1024):
                digest.update(block)
                now = time.monotonic()
                if now >= attempt_deadline:
                    raise AnalysisFailure("ATTEMPT_TIMEOUT")
                if now - last_check >= CHECK_INTERVAL_SECONDS:
                    _progress_check(maker, lease, claim)
                    last_check = now
    except OSError as exc:
        raise AnalysisFailure("VIDEO_SOURCE_UNAVAILABLE") from exc
    if digest.hexdigest() != expected_sha256:
        raise AnalysisFailure("VIDEO_SOURCE_CHANGED")


def _build_attempt_directory(
    store: MediaStore, claim: ClaimedAttempt, maker: sessionmaker[Session]
) -> Path:
    if len(claim.work_directory_name) != 32 or any(
        character not in "0123456789abcdef" for character in claim.work_directory_name
    ):
        raise AnalysisFailure("INVALID_WORK_DIRECTORY")
    parent = store.analysis / claim.task_id.hex
    with db_transaction(maker) as session:
        _lock_execution_rows(session, claim)
        parent.mkdir(parents=True, exist_ok=True)
        attempt_directory = parent / claim.work_directory_name
        if attempt_directory.parent != parent or attempt_directory.is_symlink():
            raise AnalysisFailure("INVALID_WORK_DIRECTORY")
        if not attempt_directory.resolve().is_relative_to(store.analysis.resolve()):
            raise AnalysisFailure("INVALID_WORK_DIRECTORY")
        attempt_directory.mkdir(exist_ok=False)
    return attempt_directory


def _save_manifest(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    entries: list[ManifestEntry],
    pcm_path: Path,
    config: dict[str, Any],
) -> tuple[str, set[int]]:
    asr_config = config["asr"]
    pcm_file = pcm_path.open("rb")
    db_rows: list[TranscriptSegment] = []
    fingerprint_rows: list[dict[str, Any]] = []
    try:
        for entry in entries:
            pcm_file.seek(entry.pcm_start_byte)
            audio_bytes = pcm_file.read(entry.pcm_end_byte - entry.pcm_start_byte)
            wav_path = pcm_path.parent / f"segment-{entry.segment_number:04d}.wav"
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(int(config["audio"]["channels"]))
                wav.setsampwidth(PCM_SAMPLE_WIDTH)
                wav.setframerate(int(config["audio"]["sample_rate"]))
                wav.writeframes(audio_bytes)
            wav_sha256 = _hash_path(wav_path)
            request_fp = canonical_sha256(
                {
                    "version": ASR_REQUEST_FINGERPRINT_VERSION,
                    "source_fingerprint": claim.snapshot["source"]["fingerprint"],
                    "segment_number": entry.segment_number,
                    "wav_sha256": wav_sha256,
                    "wav_bytes": wav_path.stat().st_size,
                    "model": asr_config["model"],
                    "base_url": asr_config["base_url"],
                    "parameters": asr_config["parameters"],
                }
            )
            db_rows.append(
                TranscriptSegment(
                    attempt_id=claim.attempt_id,
                    segment_number=entry.segment_number,
                    audio_start_seconds=entry.audio_start_seconds,
                    audio_end_seconds=entry.audio_end_seconds,
                    actual_audio_seconds=entry.actual_audio_seconds,
                    video_start_seconds=entry.video_start_seconds,
                    video_end_seconds=entry.video_end_seconds,
                    audio_sha256=wav_sha256,
                    request_fingerprint=request_fp,
                    status="expected",
                    request_count=0,
                )
            )
            fingerprint_rows.append(
                {
                    "segment_number": entry.segment_number,
                    "audio_start_seconds": entry.audio_start_seconds,
                    "audio_end_seconds": entry.audio_end_seconds,
                    "actual_audio_seconds": entry.actual_audio_seconds,
                    "video_start_seconds": entry.video_start_seconds,
                    "video_end_seconds": entry.video_end_seconds,
                    "audio_sha256": wav_sha256,
                    "request_fingerprint": request_fp,
                }
            )
            wav_path.unlink()
    finally:
        pcm_file.close()
    manifest_fingerprint = canonical_sha256(
        {
            "version": config["slicing"]["manifest_version"],
            "timeline_mapping_version": config["slicing"]["timeline_mapping_version"],
            "audio": config["audio"],
            "source_fingerprint": claim.snapshot["source"]["fingerprint"],
            "segments": fingerprint_rows,
        }
    )
    lease.assert_alive()
    reused_segments: set[int] = set()
    with db_transaction(maker) as session:
        _, attempt, _, _ = _lock_execution_rows(session, claim)
        prior_count = session.scalar(
            select(TranscriptSegment.id).where(TranscriptSegment.attempt_id == attempt.id).limit(1)
        )
        if prior_count is not None or attempt.manifest_fingerprint is not None:
            raise AnalysisFailure("TRANSCRIPT_MANIFEST_ALREADY_EXISTS")
        if attempt.parent_attempt_id is not None:
            parent = session.get(AnalysisAttempt, attempt.parent_attempt_id)
            if parent is None or parent.task_id != attempt.task_id:
                raise AnalysisFailure("PARENT_ATTEMPT_UNAVAILABLE")
            previous_rows = {
                item.segment_number: item
                for item in session.scalars(
                    select(TranscriptSegment).where(TranscriptSegment.attempt_id == parent.id)
                )
            }
            if parent.manifest_fingerprint == manifest_fingerprint:
                for row, expected in zip(db_rows, fingerprint_rows, strict=True):
                    prior = previous_rows.get(row.segment_number)
                    if prior is None:
                        continue
                    previous = {
                        "segment_number": prior.segment_number,
                        "audio_start_seconds": prior.audio_start_seconds,
                        "audio_end_seconds": prior.audio_end_seconds,
                        "actual_audio_seconds": prior.actual_audio_seconds,
                        "video_start_seconds": prior.video_start_seconds,
                        "video_end_seconds": prior.video_end_seconds,
                        "audio_sha256": prior.audio_sha256,
                        "request_fingerprint": prior.request_fingerprint,
                        "status": prior.status,
                        "text": prior.text,
                    }
                    if checkpoint_matches(
                        previous,
                        expected,
                        previous_manifest_fingerprint=parent.manifest_fingerprint,
                        expected_manifest_fingerprint=manifest_fingerprint,
                    ):
                        row.status = "succeeded"
                        row.text = prior.text
                        row.source_attempt_id = parent.id
                        reused_segments.add(row.segment_number)
        session.add_all(db_rows)
        attempt.manifest_fingerprint = manifest_fingerprint
    return manifest_fingerprint, reused_segments


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _start_segment_request(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    segment_number: int,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> TranscriptSegment:
    lease.assert_alive()
    timestamp = now or datetime.now(UTC)
    should_wait = False
    with db_transaction(maker) as session:
        task, attempt, _, _ = _lock_execution_rows(session, claim)
        asr_config = claim.snapshot.get("asr")
        if not isinstance(asr_config, dict):
            raise AnalysisFailure("CONFIG_SNAPSHOT_INVALID")
        if settings is not None:
            try:
                identity = model_identity_from_config(
                    asr_config,
                    default_account_reference=settings.siliconflow_account_reference,
                )
            except ValueError as exc:
                raise AnalysisFailure("MODEL_COOLDOWN_IDENTITY_INVALID") from exc
            cooldown_until = current_cooldown_until(session, identity)
            if cooldown_until is not None and cooldown_until > timestamp:
                wait_error = wait_budget_error(
                    task=task,
                    now=timestamp,
                    cooldown_until=cooldown_until,
                    single_limit_seconds=settings.model_wait_single_limit_seconds,
                    total_limit_seconds=(
                        task.model_wait_total_limit_seconds or settings.model_wait_total_limit_seconds
                    ),
                )
                if wait_error is not None:
                    require_manual_wait(task, attempt, wait_error)
                else:
                    enter_model_wait(
                        session,
                        task,
                        attempt,
                        identity=identity,
                        stage="transcribing",
                        origin="continuation",
                        cooldown_until=cooldown_until,
                        now=timestamp,
                        single_limit_seconds=settings.model_wait_single_limit_seconds,
                        total_limit_seconds=(
                            task.model_wait_total_limit_seconds or settings.model_wait_total_limit_seconds
                        ),
                    )
                should_wait = True
        if should_wait:
            segment = None
        else:
            segment = session.scalar(
                select(TranscriptSegment)
                .where(
                    TranscriptSegment.attempt_id == attempt.id,
                    TranscriptSegment.segment_number == segment_number,
                )
                .with_for_update()
            )
            if segment is None or segment.status != "expected" or segment.request_count != 0:
                raise AnalysisFailure("TRANSCRIPT_CHECKPOINT_NOT_ELIGIBLE")
            segment.request_count = 1
            segment.request_started_at = timestamp
            session.flush()
    if should_wait:
        raise ModelCooldownWait("shared model cooldown blocked the ASR request")
    if segment is None:
        raise AnalysisFailure("TRANSCRIPT_CHECKPOINT_NOT_ELIGIBLE")
    return segment


# 只有当前执行持有有效身份且片段请求仍符合预期时，才提交文本和检查点状态。
def _persist_transcript(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    segment_number: int,
    text_value: str,
    metadata: dict[str, Any],
) -> None:
    lease.assert_alive()
    with db_transaction(maker) as session:
        _, attempt, _, _ = _lock_execution_rows(session, claim)
        segment = session.scalar(
            select(TranscriptSegment)
            .where(
                TranscriptSegment.attempt_id == attempt.id,
                TranscriptSegment.segment_number == segment_number,
            )
            .with_for_update()
        )
        if segment is None or segment.status != "expected" or segment.request_count != 1:
            raise AnalysisFailure("TRANSCRIPT_CHECKPOINT_NOT_ELIGIBLE")
        # 成功文本和检查点状态在同一事务提交，未提交的模型响应不能复用。
        segment.text = text_value
        segment.status = "succeeded"
        segment.request_finished_at = datetime.now(UTC)
        segment.request_metadata = metadata
        record_analysis_event(
            session,
            task_id=claim.task_id,
            event_key=f"checkpoint-saved:{attempt.id}:{segment_number}",
            event_type="checkpoint_saved",
            attempt_id=attempt.id,
            execution_id=claim.execution_id,
            request_reference=f"transcript-segment:{attempt.id}:{segment_number}",
            stage="transcribing",
        )


def _mark_awaiting_summary(
    maker: sessionmaker[Session], lease: ExecutionLease, claim: ClaimedAttempt
) -> None:
    lease.assert_alive()
    with db_transaction(maker) as session:
        task, attempt, _, _ = _lock_execution_rows(session, claim)
        expected, completed = session.execute(
            select(
                func.count(TranscriptSegment.id),
                func.count(TranscriptSegment.id).filter(TranscriptSegment.status == "succeeded"),
            ).where(TranscriptSegment.attempt_id == attempt.id)
        ).one()
        if expected == 0 or expected != completed:
            raise AnalysisFailure("TRANSCRIPT_INCOMPLETE")
        task.status = "awaiting_summary"
        task.stage = "awaiting_summary"
        task.summary_status = "waiting"
        task.failure_code = None
        attempt.status = "awaiting_summary"
        record_analysis_event(
            session,
            task_id=task.id,
            event_key=f"transcription-completed:{attempt.id}",
            event_type="transcription_completed",
            attempt_id=attempt.id,
            execution_id=claim.execution_id,
            stage="awaiting_summary",
        )


def _fail_task(maker: sessionmaker[Session], lease: ExecutionLease, claim: ClaimedAttempt, code: str) -> None:
    lease.assert_alive()
    with db_transaction(maker) as session:
        task, attempt, video, _ = _lock_execution_rows(session, claim, allow_invalid_video=True)
        if video.status != "ready" or video.media_status != "available":
            cancellation_code = (
                "VIDEO_MEDIA_DELETION_REQUESTED"
                if video.status == "ready"
                else "VIDEO_DELETION_REQUESTED"
            )
            task.queue_entered_at = None
            task.delivery_next_check_at = None
            task.delivery_blocked_since = None
            task.delivery_wait_reason = cancellation_code
            task.model_wait_reason = None
            task.model_wait_error_code = None
            task.model_wait_stage = None
            task.model_wait_origin = None
            task.model_wait_until = None
            task.model_wait_single_deadline_at = None
            if task.status != "succeeded":
                task.status = "failed"
                task.stage = "failed"
                task.failure_code = task.failure_code or cancellation_code
            has_published_summary = session.scalar(
                select(AnalysisSummary.id).where(AnalysisSummary.task_id == task.id).limit(1)
            ) is not None
            if not has_published_summary and task.summary_status != "succeeded":
                if task.summary_status in {"running", "waiting"} or claim.execution_mode == "summary_only":
                    task.summary_status = "failed"
                unfinished_requests = list(
                    session.scalars(
                        select(SummaryRequest)
                        .where(
                            SummaryRequest.attempt_id == attempt.id,
                            SummaryRequest.status.in_(("started", "response_received")),
                        )
                        .with_for_update()
                    )
                )
                for request in unfinished_requests:
                    request.status = "failed"
                    request.error_code = cancellation_code
                    request.finished_at = datetime.now(UTC)
            if attempt.status != "succeeded":
                attempt.status = "failed"
                attempt.failure_code = attempt.failure_code or cancellation_code
                attempt.finished_at = attempt.finished_at or datetime.now(UTC)
            return
        task.status = "failed"
        task.stage = "failed"
        task.failure_code = task.failure_code or code[:64]
        task.delivery_next_check_at = datetime.now(UTC)
        task.delivery_wait_reason = "automatic_recovery_pending"
        record_analysis_event(
            session,
            task_id=task.id,
            event_key=f"execution-failed:{claim.execution_id}:{code[:64]}",
            event_type="execution_failed",
            attempt_id=attempt.id,
            execution_id=claim.execution_id,
            stage=task.stage,
            error_code=code,
        )
        if code in MODEL_BACKPRESSURE_ERROR_CODES:
            record_analysis_event(
                session,
                task_id=task.id,
                event_key=f"model-backpressure:{claim.execution_id}:{code}",
                event_type="model_rate_limited" if code.endswith("RATE_LIMITED") else "model_overloaded",
                attempt_id=attempt.id,
                execution_id=claim.execution_id,
                stage=task.stage,
                error_code=code,
            )
        if task.summary_status == "running" or claim.execution_mode == "summary_only":
            task.summary_status = "failed"
            unfinished_requests = list(
                session.scalars(
                    select(SummaryRequest)
                    .where(
                        SummaryRequest.attempt_id == attempt.id,
                        SummaryRequest.status.in_(("started", "response_received")),
                    )
                    .with_for_update()
                )
            )
            for request in unfinished_requests:
                request.status = "failed"
                request.error_code = code[:64]
                request.finished_at = datetime.now(UTC)
        attempt.status = "failed"
        attempt.failure_code = attempt.failure_code or code[:64]


def _work_directory(store: MediaStore, claim: ClaimedAttempt) -> Path:
    if len(claim.work_directory_name) != 32 or any(
        character not in "0123456789abcdef" for character in claim.work_directory_name
    ):
        raise AnalysisFailure("INVALID_WORK_DIRECTORY")
    path = store.analysis / claim.task_id.hex / claim.work_directory_name
    if path.parent != store.analysis / claim.task_id.hex or path.is_symlink():
        raise AnalysisFailure("INVALID_WORK_DIRECTORY")
    if not path.resolve(strict=False).is_relative_to(store.analysis.resolve()):
        raise AnalysisFailure("INVALID_WORK_DIRECTORY")
    return path


def _cleanup_work_directory_for_inline_tests(
    store: MediaStore,
    claim: ClaimedAttempt,
    maker: sessionmaker[Session],
    lease: ExecutionLease,
) -> str:
    path = _work_directory(store, claim)
    if not path.exists():
        return "cleaned"
    try:
        for root, dirs, files in os.walk(path, topdown=False, followlinks=False):
            root_path = Path(root)
            for filename in files:
                _validate_execution(maker, lease, claim, allow_invalid_video=True)
                candidate = root_path / filename
                if candidate.is_symlink() or candidate.resolve().is_relative_to(store.analysis.resolve()):
                    candidate.unlink()
            for directory in dirs:
                _validate_execution(maker, lease, claim, allow_invalid_video=True)
                candidate = root_path / directory
                if candidate.is_symlink():
                    candidate.unlink()
                else:
                    candidate.rmdir()
        path.rmdir()
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return "cleaned"
    except OSError:
        return "failed"


def _confirm_inline_test_stop(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    *,
    cleanup_state: str,
    reason: str,
) -> None:
    """Legacy inline confirmation is available only to the isolated test fixture path."""
    if os.environ.get("APP_ENV") != "test" or os.environ.get("ANALYSIS_TEST_INLINE_SUPERVISOR") != "1":
        raise BoundaryUnknown("inline test confirmation is disabled")
    lease.assert_alive()
    now = datetime.now(UTC)
    with db_transaction(maker) as session:
        task, attempt, _, execution = _lock_execution_rows(session, claim, allow_invalid_video=True)
        if task.status == "running":
            task.status = "failed"
            task.stage = "failed"
            task.failure_code = task.failure_code or "WORKER_STOPPED_UNFINISHED"
        if task.summary_status == "running":
            task.summary_status = "failed"
        if attempt.status == "running":
            attempt.status = "failed"
            attempt.failure_code = attempt.failure_code or "WORKER_STOPPED_UNFINISHED"
        attempt.cleanup_state = cleanup_state
        attempt.cleanup_error_code = "ATTEMPT_TEMP_CLEANUP_FAILED" if cleanup_state == "failed" else None
        attempt.stop_confirmed_at = now
        attempt.finished_at = now
        execution.stop_confirmed_at = now
        execution.stop_confirmation_source = "test_inline_synthetic"
        execution.stop_reason = reason[:64]


def _deletion_cleanup_is_pending(maker: sessionmaker[Session], claim: ClaimedAttempt) -> bool:
    with maker() as session:
        row = session.execute(
            select(Video.status, Video.media_status).where(Video.id == claim.video_id)
        ).one_or_none()
        session.commit()
    return row is None or row.status != "ready" or row.media_status != "available"


def _create_wav_segments(
    pcm_path: Path,
    entries: list[ManifestEntry],
    *,
    channels: int,
    sample_rate: int,
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
) -> list[Path]:
    result: list[Path] = []
    with pcm_path.open("rb") as pcm:
        for item in entries:
            _progress_check(maker, lease, claim)
            pcm.seek(item.pcm_start_byte)
            raw_audio = pcm.read(item.pcm_end_byte - item.pcm_start_byte)
            path = pcm_path.parent / f"segment-{item.segment_number:04d}.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(channels)
                wav.setsampwidth(PCM_SAMPLE_WIDTH)
                wav.setframerate(sample_rate)
                wav.writeframes(raw_audio)
            result.append(path)
    return result


def _read_asr_result(path: Path, return_code: int, max_bytes: int) -> tuple[str, dict[str, Any]]:
    try:
        if path.stat().st_size > max_bytes:
            raise AnalysisFailure("ASR_RESPONSE_TOO_LARGE")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisFailure("ASR_INVALID_RESPONSE") from exc
    code = payload.get("error_code") if isinstance(payload, dict) else None
    if isinstance(code, str):
        if len(code) > 64 or not code.replace("_", "").isalnum():
            raise AnalysisFailure("ASR_INVALID_RESPONSE")
        retry_after = payload.get("retry_after") if code in ASR_BACKPRESSURE_ERROR_CODES else None
        if not isinstance(retry_after, str) or len(retry_after) > 128:
            retry_after = None
        raise AnalysisFailure(
            code,
            retry_after=retry_after,
            response_received_at=(
                _response_received_at(payload) if code in ASR_BACKPRESSURE_ERROR_CODES else None
            ),
        )
    if return_code != 0:
        raise AnalysisFailure("ASR_REQUEST_FAILED")
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise AnalysisFailure("ASR_INVALID_RESPONSE")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise AnalysisFailure("ASR_INVALID_RESPONSE")
    return payload["text"], metadata


# 按清单逐片生成音频并调用识别服务，成功片段及时落库供恢复复用。
def _transcribe(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    settings: Settings,
    store: MediaStore,
    attempt_deadline: float,
) -> None:
    config = claim.snapshot
    if canonical_sha256(config) != _attempt_config_hash(maker, claim):
        raise AnalysisFailure("CONFIG_SNAPSHOT_INVALID")
    if config.get("version") != "analysis-config-v1":
        raise AnalysisFailure("CONFIG_VERSION_UNSUPPORTED")
    source_config = config["source"]
    source_path = store.target_path(claim.storage_name)
    _hash_source(
        source_path,
        int(source_config["byte_count"]),
        str(source_config["sha256"]),
        maker=maker,
        lease=lease,
        claim=claim,
        attempt_deadline=attempt_deadline,
    )
    directory = _build_attempt_directory(store, claim, maker)
    pcm_path = directory / "audio.s16le"
    _set_stage(maker, lease, claim, "extracting_audio")
    extraction_seconds = min(
        float(config["limits"]["media_total_seconds"]), attempt_deadline - time.monotonic()
    )
    _run_process(
        [
            settings.ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(source_path),
            "-map",
            f"0:{int(source_config['audio_stream_index'])}",
            "-vn",
            "-ac",
            str(int(config["audio"]["channels"])),
            "-ar",
            str(int(config["audio"]["sample_rate"])),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "-y",
            str(pcm_path),
        ],
        timeout_seconds=extraction_seconds,
        grace_seconds=float(config["limits"]["process_stop_grace_seconds"]),
        maker=maker,
        lease=lease,
        claim=claim,
        attempt_deadline=attempt_deadline,
    )
    if not pcm_path.is_file() or pcm_path.is_symlink():
        raise AnalysisFailure("EXTRACTED_AUDIO_INVALID")
    _set_stage(maker, lease, claim, "preparing_manifest")
    entries = build_manifest(
        pcm_bytes=pcm_path.stat().st_size,
        duration_seconds=float(source_config["duration_seconds"]),
        sample_rate=int(config["audio"]["sample_rate"]),
        channels=int(config["audio"]["channels"]),
        segment_seconds=int(config["slicing"]["segment_seconds"]),
    )
    _, reused_segments = _save_manifest(maker, lease, claim, entries, pcm_path, config)
    wav_paths = _create_wav_segments(
        pcm_path,
        entries,
        channels=int(config["audio"]["channels"]),
        sample_rate=int(config["audio"]["sample_rate"]),
        maker=maker,
        lease=lease,
        claim=claim,
    )
    _set_stage(maker, lease, claim, "transcribing")
    asr_config = config["asr"]
    limits = config["limits"]
    for entry, wav_path in zip(entries, wav_paths, strict=True):
        if entry.segment_number in reused_segments:
            continue
        if time.monotonic() >= attempt_deadline:
            raise AnalysisFailure("ATTEMPT_TIMEOUT")
        segment_request = _start_segment_request(maker, lease, claim, entry.segment_number, settings=settings)
        response_path = directory / f"response-{entry.segment_number:04d}.json"
        request_timeout = min(float(limits["http_total_seconds"]), attempt_deadline - time.monotonic())
        _run_process(
            [
                sys.executable,
                "-m",
                "videoclip.asr_client",
                "--audio",
                str(wav_path),
                "--output",
                str(response_path),
                "--base-url",
                str(asr_config["base_url"]),
                "--model",
                str(asr_config["model"]),
                "--total-timeout",
                str(request_timeout),
                "--connect-timeout",
                str(min(float(limits["http_connect_seconds"]), request_timeout)),
                "--read-timeout",
                str(min(float(limits["http_read_seconds"]), request_timeout)),
                "--maximum-response-bytes",
                str(int(limits["response_max_bytes"])),
                "--parameters-json",
                json.dumps(asr_config["parameters"], separators=(",", ":")),
            ],
            timeout_seconds=request_timeout,
            grace_seconds=float(limits["process_stop_grace_seconds"]),
            maker=maker,
            lease=lease,
            claim=claim,
            attempt_deadline=attempt_deadline,
            timeout_error_code="ASR_TIMEOUT",
        )
        result_text, metadata = _read_asr_result(response_path, 0, int(limits["response_max_bytes"]))
        _persist_transcript(maker, lease, claim, entry.segment_number, result_text, metadata)
        identity = model_identity_from_config(
            asr_config,
            default_account_reference=settings.siliconflow_account_reference,
        )
        record_model_success(
            maker,
            identity,
            request_started_at=segment_request.request_started_at or datetime.now(UTC),
        )
        response_path.unlink(missing_ok=True)
    _mark_awaiting_summary(maker, lease, claim)


def _summary_transcript(
    maker: sessionmaker[Session], lease: ExecutionLease, claim: ClaimedAttempt
) -> tuple[list[dict[str, Any]], str, float]:
    lease.assert_alive()
    with maker() as session:
        _, attempt, video, _ = _lock_execution_rows(session, claim)
        rows = list(
            session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.attempt_id == attempt.id)
                .order_by(TranscriptSegment.segment_number)
            )
        )
        row_values = [
            {
                "segment_number": row.segment_number,
                "audio_start_seconds": row.audio_start_seconds,
                "audio_end_seconds": row.audio_end_seconds,
                "actual_audio_seconds": row.actual_audio_seconds,
                "video_start_seconds": row.video_start_seconds,
                "video_end_seconds": row.video_end_seconds,
                "audio_sha256": row.audio_sha256,
                "request_fingerprint": row.request_fingerprint,
                "status": row.status,
                "text": row.text,
            }
            for row in rows
        ]
        try:
            segments, transcript_fingerprint = validate_transcript_manifest(
                row_values,
                snapshot=claim.snapshot,
                video_duration_seconds=video.duration_seconds,
                expected_manifest_fingerprint=attempt.manifest_fingerprint,
            )
        except SummaryFailure as exc:
            raise AnalysisFailure(exc.code) from exc
        duration = video.duration_seconds
        session.commit()
    lease.assert_alive()
    return segments, transcript_fingerprint, duration


def _start_summary_request(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    contract: Any,
    request_fingerprint: str,
    *,
    settings: Settings,
    correction_error_code: str | None,
    now: datetime | None = None,
) -> SummaryRequest:
    lease.assert_alive()
    timestamp = now or datetime.now(UTC)
    should_wait = False
    record: SummaryRequest | None = None
    with db_transaction(maker) as session:
        task, attempt, _, execution = _lock_execution_rows(session, claim)
        if task.summary_status != "running" or task.stage != "summarizing":
            raise AnalysisFailure("SUMMARY_EXECUTION_NOT_ELIGIBLE")
        summary_config = claim.snapshot.get("summary")
        if not isinstance(summary_config, dict):
            raise AnalysisFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
        try:
            identity = model_identity_from_config(
                summary_config,
                default_account_reference=settings.siliconflow_account_reference,
            )
        except ValueError as exc:
            raise AnalysisFailure("MODEL_COOLDOWN_IDENTITY_INVALID") from exc
        cooldown_until = current_cooldown_until(session, identity)
        if cooldown_until is not None and cooldown_until > timestamp:
            wait_error = wait_budget_error(
                task=task,
                now=timestamp,
                cooldown_until=cooldown_until,
                single_limit_seconds=settings.model_wait_single_limit_seconds,
                total_limit_seconds=(
                    task.model_wait_total_limit_seconds or settings.model_wait_total_limit_seconds
                ),
            )
            if wait_error is not None:
                require_manual_wait(task, attempt, wait_error)
            else:
                enter_model_wait(
                    session,
                    task,
                    attempt,
                    identity=identity,
                    stage="summarizing",
                    origin="continuation",
                    cooldown_until=cooldown_until,
                    now=timestamp,
                    single_limit_seconds=settings.model_wait_single_limit_seconds,
                    total_limit_seconds=(
                        task.model_wait_total_limit_seconds or settings.model_wait_total_limit_seconds
                    ),
                )
            should_wait = True
        if not should_wait:
            attempt_request_count = session.scalar(
                select(func.count(SummaryRequest.id)).where(SummaryRequest.attempt_id == attempt.id)
            )
            task_request_count = session.scalar(
                select(func.count(SummaryRequest.id)).where(SummaryRequest.task_id == task.id)
            )
            request_number = int(attempt_request_count or 0) + 1
            if int(task_request_count or 0) >= contract.max_requests:
                raise AnalysisFailure("SUMMARY_REPAIR_EXHAUSTED")
            record = SummaryRequest(
                task_id=task.id,
                attempt_id=attempt.id,
                execution_id=execution.id,
                request_number=request_number,
                request_fingerprint=request_fingerprint,
                status="started",
                provider=contract.provider,
                model=contract.model,
                prompt_version=contract.prompt_version,
                started_at=timestamp,
                correction_error_code=correction_error_code,
            )
            session.add(record)
            session.flush()
            record_analysis_event(
                session,
                task_id=task.id,
                event_key=f"summary-request-started:{record.id}",
                event_type="summary_requested",
                attempt_id=attempt.id,
                execution_id=execution.id,
                request_reference=f"summary-request:{record.id}",
                stage="summarizing",
            )
            session.expunge(record)
    if should_wait:
        raise ModelCooldownWait("shared model cooldown blocked the summary request")
    if record is None:
        raise AnalysisFailure("SUMMARY_REQUEST_AUDIT_INVALID")
    return record


def _record_summary_response(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    request_id: uuid.UUID,
    metadata: dict[str, Any],
) -> None:
    lease.assert_alive()
    with db_transaction(maker) as session:
        _lock_execution_rows(session, claim)
        record = session.scalar(
            select(SummaryRequest).where(SummaryRequest.id == request_id).with_for_update()
        )
        if record is None or record.attempt_id != claim.attempt_id or record.status != "started":
            raise AnalysisFailure("SUMMARY_REQUEST_AUDIT_INVALID")
        record.status = "response_received"
        record.finished_at = datetime.now(UTC)
        record.elapsed_seconds = float(metadata["elapsed_seconds"])
        record.finish_reason = metadata.get("finish_reason")
        record.provider_trace_id = metadata.get("trace_id")
        record.usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
        record.response_bytes = int(metadata["response_bytes"])
        record_analysis_event(
            session,
            task_id=claim.task_id,
            event_key=f"summary-response:{record.id}",
            event_type="summary_response_received",
            attempt_id=claim.attempt_id,
            execution_id=claim.execution_id,
            request_reference=f"summary-request:{record.id}",
            stage="summarizing",
        )


def _record_summary_failure(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    request_id: uuid.UUID,
    error_code: str,
    *,
    elapsed_seconds: float,
    validation_error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    lease.assert_alive()
    with db_transaction(maker) as session:
        _lock_execution_rows(session, claim)
        record = session.scalar(
            select(SummaryRequest).where(SummaryRequest.id == request_id).with_for_update()
        )
        if record is None or record.attempt_id != claim.attempt_id:
            raise AnalysisFailure("SUMMARY_REQUEST_AUDIT_INVALID")
        record.status = "failed"
        record.error_code = error_code[:64]
        record.validation_error_code = validation_error_code[:64] if validation_error_code else None
        if validation_error_code and is_repairable_summary_failure(validation_error_code):
            record.correction_error_code = validation_error_code[:64]
        record.finished_at = datetime.now(UTC)
        record.elapsed_seconds = max(0.0, float(elapsed_seconds))
        if metadata is not None:
            record.finish_reason = metadata.get("finish_reason")
            record.provider_trace_id = metadata.get("trace_id")
            record.usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
            record.response_bytes = int(metadata.get("response_bytes", 0))
        record_analysis_event(
            session,
            task_id=claim.task_id,
            event_key=f"summary-failed:{record.id}",
            event_type="summary_failed",
            attempt_id=claim.attempt_id,
            execution_id=claim.execution_id,
            request_reference=f"summary-request:{record.id}",
            stage="summarizing",
            error_code=error_code,
        )


# 再次核对当前尝试和执行身份，并依靠任务唯一约束避免重复发布摘要。
def _publish_summary(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    request_id: uuid.UUID,
    *,
    contract: Any,
    expected_transcript_fingerprint: str,
    payload: dict[str, Any],
) -> None:
    lease.assert_alive()
    with db_transaction(maker) as session:
        task, attempt, video, execution = _lock_execution_rows(session, claim)
        if task.summary_status != "running" or task.status != "running":
            raise AnalysisFailure("SUMMARY_EXECUTION_NOT_ELIGIBLE")
        request = session.scalar(
            select(SummaryRequest).where(SummaryRequest.id == request_id).with_for_update()
        )
        if (
            request is None
            or request.task_id != task.id
            or request.attempt_id != attempt.id
            or request.execution_id != execution.id
            or request.status != "response_received"
        ):
            raise AnalysisFailure("SUMMARY_REQUEST_AUDIT_INVALID")
        existing = session.scalar(
            select(AnalysisSummary.id).where(AnalysisSummary.task_id == task.id).limit(1)
        )
        if existing is not None:
            raise AnalysisFailure("SUMMARY_ALREADY_PUBLISHED")
        row_values = [
            {
                "segment_number": row.segment_number,
                "audio_start_seconds": row.audio_start_seconds,
                "audio_end_seconds": row.audio_end_seconds,
                "actual_audio_seconds": row.actual_audio_seconds,
                "video_start_seconds": row.video_start_seconds,
                "video_end_seconds": row.video_end_seconds,
                "audio_sha256": row.audio_sha256,
                "request_fingerprint": row.request_fingerprint,
                "status": row.status,
                "text": row.text,
            }
            for row in session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.attempt_id == attempt.id)
                .order_by(TranscriptSegment.segment_number)
            )
        ]
        try:
            _, transcript_fingerprint = validate_transcript_manifest(
                row_values,
                snapshot=claim.snapshot,
                video_duration_seconds=video.duration_seconds,
                expected_manifest_fingerprint=attempt.manifest_fingerprint,
            )
        except SummaryFailure as exc:
            raise AnalysisFailure(exc.code) from exc
        if transcript_fingerprint != expected_transcript_fingerprint or video.status != "ready":
            raise AnalysisFailure("TRANSCRIPT_SOURCE_MISMATCH")
        payload_hash = summary_canonical_sha256(payload)
        session.add(
            AnalysisSummary(
                task_id=task.id,
                video_id=video.id,
                attempt_id=attempt.id,
                manifest_fingerprint=str(attempt.manifest_fingerprint),
                transcript_fingerprint=transcript_fingerprint,
                structure_version=contract.schema_version,
                provider=contract.provider,
                model=contract.model,
                prompt_version=contract.prompt_version,
                prompt_sha256=contract.template_sha256,
                response_schema_sha256=contract.response_schema_sha256,
                payload_sha256=payload_hash,
                payload=payload,
            )
        )
        request.status = "succeeded"
        attempt.status = "succeeded"
        attempt.failure_code = None
        attempt.cleanup_state = "pending"
        attempt.cleanup_error_code = None
        task.status = "succeeded"
        task.stage = "succeeded"
        task.summary_status = "succeeded"
        task.failure_code = None
        record_analysis_event(
            session,
            task_id=task.id,
            event_key=f"summary-published:{request.id}",
            event_type="summary_published",
            attempt_id=attempt.id,
            execution_id=execution.id,
            request_reference=f"summary-request:{request.id}",
            stage="succeeded",
        )


def _read_llm_result(path: Path, maximum_bytes: int) -> tuple[str, dict[str, Any]]:
    try:
        if path.is_symlink() or path.stat().st_size > maximum_bytes + 8192:
            raise AnalysisFailure("LLM_RESPONSE_TOO_LARGE")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisFailure("LLM_INVALID_RESPONSE") from exc
    code = payload.get("error_code") if isinstance(payload, dict) else None
    if isinstance(code, str):
        if len(code) > 64 or not code.replace("_", "").isalnum():
            raise AnalysisFailure("LLM_INVALID_RESPONSE")
        retry_after = payload.get("retry_after") if code in LLM_BACKPRESSURE_ERROR_CODES else None
        if not isinstance(retry_after, str) or len(retry_after) > 128:
            retry_after = None
        raise AnalysisFailure(
            code,
            retry_after=retry_after,
            response_received_at=(
                _response_received_at(payload) if code in LLM_BACKPRESSURE_ERROR_CODES else None
            ),
        )
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("content"), str)
        or not isinstance(payload.get("metadata"), dict)
    ):
        raise AnalysisFailure("LLM_INVALID_RESPONSE")
    metadata = payload["metadata"]
    elapsed = metadata.get("elapsed_seconds")
    response_bytes = metadata.get("response_bytes")
    if (
        not isinstance(elapsed, int | float)
        or isinstance(elapsed, bool)
        or not math.isfinite(float(elapsed))
        or elapsed < 0
        or not isinstance(response_bytes, int)
        or isinstance(response_bytes, bool)
        or response_bytes < 0
        or response_bytes > maximum_bytes
    ):
        raise AnalysisFailure("LLM_INVALID_RESPONSE")
    return payload["content"], metadata


# 读取已核验转写和冻结配置后调用摘要服务，模型返回仍须经过后端契约校验。
def _summarize(
    maker: sessionmaker[Session],
    lease: ExecutionLease,
    claim: ClaimedAttempt,
    store: MediaStore,
    attempt_deadline: float,
    settings: Settings,
) -> None:
    _set_stage(maker, lease, claim, "summarizing")
    try:
        contract = resolve_summary_contract(claim.snapshot)
    except SummaryFailure as exc:
        raise AnalysisFailure(exc.code) from exc
    segments, transcript_fingerprint, duration = _summary_transcript(maker, lease, claim)
    source_config = claim.snapshot["source"]
    source_path = store.target_path(claim.storage_name)
    _hash_source(
        source_path,
        int(source_config["byte_count"]),
        str(source_config["sha256"]),
        maker=maker,
        lease=lease,
        claim=claim,
        attempt_deadline=attempt_deadline,
    )
    directory = _work_directory(store, claim)
    if not directory.is_dir():
        if claim.execution_mode != "summary_only":
            raise AnalysisFailure("SUMMARY_WORK_DIRECTORY_UNAVAILABLE")
        directory = _build_attempt_directory(store, claim, maker)
    try:
        validate_transcript_context_budget(segments, contract)
        prompt = build_summary_prompt(segments, contract.prompt_version)
        validate_context_budget(prompt, contract)
    except SummaryFailure as exc:
        raise AnalysisFailure(exc.code) from exc
    correction_code: str | None = None
    with maker() as session:
        previous_request = session.scalar(
            select(SummaryRequest)
            .where(SummaryRequest.task_id == claim.task_id)
            .order_by(SummaryRequest.started_at.desc(), SummaryRequest.id.desc())
            .limit(1)
        )
        if previous_request is not None and is_repairable_summary_failure(
            previous_request.correction_error_code or ""
        ):
            correction_code = previous_request.correction_error_code
        session.commit()
    for request_number in range(1, contract.max_requests + 1):
        remaining = attempt_deadline - time.monotonic()
        if remaining <= 0:
            raise AnalysisFailure("ATTEMPT_TIMEOUT")
        request_fingerprint = summary_request_fingerprint(
            contract=contract,
            transcript_fingerprint=transcript_fingerprint,
            correction_code=correction_code,
        )
        request_record = _start_summary_request(
            maker,
            lease,
            claim,
            contract,
            request_fingerprint,
            settings=settings,
            correction_error_code=correction_code,
        )
        request_path = directory / f"summary-request-{request_record.request_number:02d}.json"
        response_path = directory / f"summary-response-{request_record.request_number:02d}.json"
        request_total = min(contract.timeouts["total"], remaining)
        messages = [
            {"role": "system", "content": "输出严格 JSON。不得臆造引文或时间。"},
            {
                "role": "user",
                "content": prompt
                + (
                    "\n校验错误，请只修正这些问题：\n" + correction_for_failure(correction_code)
                    if correction_code
                    else ""
                ),
            },
        ]
        request_document = {
            "base_url": contract.base_url,
            "model": contract.model,
            "messages": messages,
            "temperature": contract.temperature,
            "max_output_tokens": contract.max_output_tokens,
            "enable_thinking": contract.enable_thinking,
            "timeouts": {
                **contract.timeouts,
                "total": request_total,
                "connect": min(contract.timeouts["connect"], request_total),
                "read": min(contract.timeouts["read"], request_total),
                "write": min(contract.timeouts["write"], request_total),
                "pool": min(contract.timeouts["pool"], request_total),
            },
            "max_response_bytes": contract.max_response_bytes,
            "max_diagnostic_bytes": contract.max_diagnostic_bytes,
        }
        if request_path.is_symlink() or response_path.is_symlink():
            raise AnalysisFailure("SUMMARY_WORK_DIRECTORY_INVALID")
        try:
            request_path.write_text(
                json.dumps(request_document, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError as exc:
            raise AnalysisFailure("SUMMARY_WORK_DIRECTORY_UNAVAILABLE") from exc
        started = time.monotonic()
        try:
            _run_process(
                [
                    sys.executable,
                    "-m",
                    "videoclip.llm_client",
                    "--request",
                    str(request_path),
                    "--output",
                    str(response_path),
                ],
                timeout_seconds=request_total,
                grace_seconds=float(claim.snapshot["limits"]["process_stop_grace_seconds"]),
                maker=maker,
                lease=lease,
                claim=claim,
                attempt_deadline=attempt_deadline,
                timeout_error_code="LLM_TOTAL_TIMEOUT",
            )
            raw, metadata = _read_llm_result(response_path, contract.max_response_bytes)
        except AnalysisFailure as exc:
            if exc.code in {
                "EXECUTION_OWNERSHIP_LOST",
                "VIDEO_UNAVAILABLE",
                "PROCESS_STOP_UNCONFIRMED",
                "ATTEMPT_TIMEOUT",
            }:
                raise
            error_code = "LLM_TOTAL_TIMEOUT" if exc.code == "LOCAL_PROCESS_TIMEOUT" else exc.code
            _record_summary_failure(
                maker,
                lease,
                claim,
                request_record.id,
                error_code,
                elapsed_seconds=time.monotonic() - started,
            )
            raise AnalysisFailure(
                error_code,
                retry_after=exc.retry_after,
                response_received_at=exc.response_received_at,
            ) from exc
        finally:
            request_path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)

        _record_summary_response(maker, lease, claim, request_record.id, metadata)
        summary_config = claim.snapshot.get("summary")
        if not isinstance(summary_config, dict):
            raise AnalysisFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
        try:
            identity = model_identity_from_config(
                summary_config,
                default_account_reference=settings.siliconflow_account_reference,
            )
        except ValueError as exc:
            raise AnalysisFailure("MODEL_COOLDOWN_IDENTITY_INVALID") from exc
        record_model_success(maker, identity, request_started_at=request_record.started_at)
        finish_reason = metadata.get("finish_reason")
        if finish_reason == "length":
            error_code = "LLM_OUTPUT_TRUNCATED"
            _record_summary_failure(
                maker,
                lease,
                claim,
                request_record.id,
                error_code,
                validation_error_code=error_code,
                elapsed_seconds=float(metadata["elapsed_seconds"]),
                metadata=metadata,
            )
            raise AnalysisFailure(error_code)
        if finish_reason != "stop":
            error_code = "LLM_FINISH_REASON_UNSUPPORTED"
            _record_summary_failure(
                maker,
                lease,
                claim,
                request_record.id,
                error_code,
                elapsed_seconds=float(metadata["elapsed_seconds"]),
                metadata=metadata,
            )
            raise AnalysisFailure(error_code)
        try:
            result = validate_summary_output(raw, segments, video_duration_seconds=duration)
        except SummaryFailure as exc:
            _record_summary_failure(
                maker,
                lease,
                claim,
                request_record.id,
                "SUMMARY_OUTPUT_INVALID",
                validation_error_code=exc.code,
                elapsed_seconds=float(metadata["elapsed_seconds"]),
                metadata=metadata,
            )
            if is_repairable_summary_failure(exc.code) and request_number < contract.max_requests:
                correction_code = exc.code
                continue
            raise AnalysisFailure(
                "SUMMARY_REPAIR_EXHAUSTED" if is_repairable_summary_failure(exc.code) else exc.code
            ) from exc
        try:
            _publish_summary(
                maker,
                lease,
                claim,
                request_record.id,
                contract=contract,
                expected_transcript_fingerprint=transcript_fingerprint,
                payload=result,
            )
        except SQLAlchemyError as exc:
            raise AnalysisFailure("SUMMARY_PUBLISH_FAILED") from exc
        return
    raise AnalysisFailure("SUMMARY_REPAIR_EXHAUSTED")


def _attempt_config_hash(maker: sessionmaker[Session], claim: ClaimedAttempt) -> str:
    with maker() as session:
        value = session.scalar(
            select(AnalysisAttempt.config_sha256).where(AnalysisAttempt.id == claim.attempt_id)
        )
        session.commit()
    if not isinstance(value, str):
        raise AnalysisFailure("CONFIG_SNAPSHOT_INVALID")
    return value


def _get_or_make_worker_components(
    settings: Settings,
) -> tuple[Engine, MediaStore]:
    engine = create_database_engine(settings)
    store = MediaStore(settings.app_data_root)
    try:
        store.prepare()
    except OSError:
        engine.dispose()
        raise
    return engine, store


@shared_task(  # type: ignore[misc]
    name="videoclip.transcribe_analysis",
    bind=False,
    autoretry_for=(),
    retry_backoff=False,
    max_retries=0,
    acks_late=False,
    reject_on_worker_lost=False,
    ignore_result=True,
    soft_time_limit=10_830,
    time_limit=10_860,
)
# 后台任务入口解析任务与尝试编号，领取执行位后才进入转写或摘要阶段。
def transcribe_analysis(task_id_value: str, attempt_id_value: str | None = None) -> dict[str, str]:
    if attempt_id_value is None:
        return {"status": "ignored_legacy_task_message"}
    try:
        task_id = uuid.UUID(task_id_value)
        attempt_id = uuid.UUID(attempt_id_value)
    except ValueError:
        return {"status": "ignored_invalid_message_id"}
    settings = get_settings()
    inline_test_supervisor = (
        settings.app_env == "test" and os.environ.get("ANALYSIS_TEST_INLINE_SUPERVISOR") == "1"
    )
    boundary: WorkerBoundary | None = None
    if not inline_test_supervisor:
        try:
            boundary = establish_execution_boundary()
        except BoundaryUnknown as exc:
            logger.error("analysis_supervision_unavailable reason=%s", exc)
            return {"status": "stop_confirmation_required"}
    engine, store = _get_or_make_worker_components(settings)
    lease: ExecutionLease | None = None
    claim: ClaimedAttempt | None = None
    terminal_reason = "worker_error"
    failure_code: str | None = None
    try:
        lease = ExecutionLease.acquire(engine)
        if lease is None:
            return {"status": "global_execution_busy"}
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        try:
            # 队列消息只是唤醒信号；领取时仍需重新核对任务顺序、当前尝试和执行身份。
            claim = _claim_attempt(lease, task_id, attempt_id, worker_id, boundary)
        except IntegrityError:
            return {"status": "global_execution_busy"}
        if claim is None:
            return {"status": "ignored_not_eligible"}
        maker = sessionmaker(bind=lease.connection, autoflush=False, expire_on_commit=False)
        if not inline_test_supervisor:
            try:
                register_execution_with_supervisor(_execution_identity(claim))
            except BoundaryUnknown as exc:
                logger.error(
                    "analysis_supervisor_registration_required execution_id=%s reason=%s",
                    claim.execution_id,
                    exc,
                )
                return {"status": "stop_confirmation_required"}
        attempt_deadline = time.monotonic() + int(claim.snapshot["limits"]["attempt_total_seconds"])
        try:
            # 摘要续跑复用已持久化的完整转写，不重新切片或重复调用 ASR。
            if claim.execution_mode != "summary_only":
                _transcribe(maker, lease, claim, settings, store, attempt_deadline)
            _summarize(maker, lease, claim, store, attempt_deadline, settings)
            terminal_reason = "summary_published"
        except ModelCooldownWait:
            terminal_reason = "model_cooldown_wait"
        except StopUnconfirmed:
            logger.error(
                "analysis_stop_unconfirmed task_id=%s attempt_id=%s", task_id.hex, claim.attempt_id.hex
            )
            return {"status": "stop_unconfirmed"}
        except AnalysisFailure as exc:
            failure_code = exc.code
            terminal_reason = exc.code
            if exc.code in MODEL_BACKPRESSURE_ERROR_CODES:
                # 限流或过载响应按服务商返回时刻更新共享冷却，实际失败状态仍由后续事务记录。
                try:
                    identity = _backpressure_identity(claim, exc.code, settings)
                    update = extend_shared_cooldown(
                        maker,
                        identity,
                        now=datetime.now(UTC),
                        retry_after=exc.retry_after,
                        response_received_at=exc.response_received_at,
                        random_fraction=random.random(),
                        policy=_cooldown_policy(settings),
                    )
                    logger.info(
                        "shared_model_cooldown_extended provider=%s account_reference=%s model=%s "
                        "error_code=%s until=%s consecutive_backpressure_responses=%s "
                        "retry_after_accepted=%s",
                        identity.provider,
                        identity.account_reference,
                        identity.model,
                        exc.code,
                        update.cooldown_until.isoformat(),
                        update.consecutive_backpressure_responses,
                        update.retry_after_accepted,
                    )
                except Exception:
                    failure_code = "MODEL_COOLDOWN_PERSIST_FAILED"
                    terminal_reason = failure_code
                    logger.error("shared_model_cooldown_write_failed task_id=%s", task_id.hex)
        except (OSError, SQLAlchemyError):
            failure_code = "ANALYSIS_STORAGE_OR_DATABASE_FAILED"
            terminal_reason = failure_code
        except Exception:
            failure_code = "ANALYSIS_INTERNAL_ERROR"
            terminal_reason = failure_code
            logger.error("analysis_worker_failed task_id=%s attempt_id=%s", task_id.hex, claim.attempt_id.hex)

        try:
            if failure_code is not None:
                _fail_task(maker, lease, claim, failure_code)
            if inline_test_supervisor:
                cleanup_state = (
                    "pending"
                    if _deletion_cleanup_is_pending(maker, claim)
                    else _cleanup_work_directory_for_inline_tests(store, claim, maker, lease)
                )
                _confirm_inline_test_stop(
                    maker,
                    lease,
                    claim,
                    cleanup_state=cleanup_state,
                    reason=terminal_reason,
                )
        except (AnalysisFailure, SQLAlchemyError):
            logger.error(
                "analysis_result_write_unconfirmed task_id=%s attempt_id=%s",
                task_id.hex,
                claim.attempt_id.hex,
            )
            return {"status": "stop_unconfirmed"}
        return (
            {"status": "succeeded" if failure_code is None else "failed"}
            if inline_test_supervisor
            else {"status": "awaiting_stop_evidence"}
        )
    finally:
        if lease is not None:
            lease.release()
        engine.dispose()
