from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from videoclip.db import create_database_engine, create_session_factory
from videoclip.events import record_analysis_event
from videoclip.models import AnalysisAttempt, AnalysisExecution, AnalysisTask, SummaryRequest, Video
from videoclip.services import db_transaction
from videoclip.settings import get_settings

PROCESS_SCOPE = "linux-session-v1"
StopSource = Literal["worker_supervisor", "operator_container_exit"]
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")


@dataclass(frozen=True)
class ExecutionIdentity:
    execution_id: uuid.UUID
    task_id: uuid.UUID
    attempt_id: uuid.UUID
    worker_instance_id: uuid.UUID
    worker_container_id: str
    process_scope: str
    boundary_pid: int
    boundary_start_ticks: int
    boundary_session_id: int
    boundary_boot_id: str


@dataclass(frozen=True)
class StopEvidence:
    identity: ExecutionIdentity
    source: StopSource
    observation: dict[str, object]
    sha256: str


# 从数据库执行记录提取进程、会话和容器身份，后续证据必须与这些字段一致。
def identity_from_execution(execution: AnalysisExecution) -> ExecutionIdentity | None:
    worker_instance_id = execution.worker_instance_id
    worker_container_id = execution.worker_container_id
    process_scope = execution.process_scope
    boundary_pid = execution.boundary_pid
    boundary_start_ticks = execution.boundary_start_ticks
    boundary_session_id = execution.boundary_session_id
    boundary_boot_id = execution.boundary_boot_id
    if (
        worker_instance_id is None
        or worker_container_id is None
        or process_scope is None
        or boundary_pid is None
        or boundary_start_ticks is None
        or boundary_session_id is None
        or boundary_boot_id is None
    ):
        return None
    return ExecutionIdentity(
        execution_id=execution.id,
        task_id=execution.task_id,
        attempt_id=execution.attempt_id,
        worker_instance_id=worker_instance_id,
        worker_container_id=worker_container_id,
        process_scope=process_scope,
        boundary_pid=boundary_pid,
        boundary_start_ticks=boundary_start_ticks,
        boundary_session_id=boundary_session_id,
        boundary_boot_id=boundary_boot_id,
    )


def _evidence_hash(identity: ExecutionIdentity, source: StopSource, observation: dict[str, object]) -> str:
    payload = {
        "execution_id": str(identity.execution_id),
        "task_id": str(identity.task_id),
        "attempt_id": str(identity.attempt_id),
        "worker_instance_id": str(identity.worker_instance_id),
        "worker_container_id": identity.worker_container_id,
        "process_scope": identity.process_scope,
        "boundary_pid": identity.boundary_pid,
        "boundary_start_ticks": identity.boundary_start_ticks,
        "boundary_session_id": identity.boundary_session_id,
        "boundary_boot_id": identity.boundary_boot_id,
        "source": source,
        "observation": observation,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# 监督器确认整组进程退出后生成绑定执行身份的证据摘要。
def worker_stop_evidence(identity: ExecutionIdentity, *, session_stopped: bool) -> StopEvidence:
    if not session_stopped or identity.process_scope != PROCESS_SCOPE:
        raise ValueError("the controlled process boundary is not proven empty")
    observation: dict[str, object] = {"session_stopped": True}
    return StopEvidence(
        identity=identity,
        source="worker_supervisor",
        observation=observation,
        sha256=_evidence_hash(identity, "worker_supervisor", observation),
    )


# 人工确认只接受目标容器的可信退出状态，并绑定对应执行身份。
def operator_container_stop_evidence(
    identity: ExecutionIdentity, *, container_id: str, exited: bool
) -> StopEvidence:
    if not exited or identity.process_scope != PROCESS_SCOPE or not _CONTAINER_ID.fullmatch(container_id):
        raise ValueError("the exact Worker container stop is not proven")
    if not identity.worker_container_id or container_id[:12] != identity.worker_container_id[:12]:
        raise ValueError("the stopped container does not match this execution's Worker instance")
    observation: dict[str, object] = {"container_id": container_id, "container_state": "exited"}
    return StopEvidence(
        identity=identity,
        source="operator_container_exit",
        observation=observation,
        sha256=_evidence_hash(identity, "operator_container_exit", observation),
    )


def _matches(execution: AnalysisExecution, identity: ExecutionIdentity) -> bool:
    return identity_from_execution(execution) == identity


# 在锁定任务、尝试和执行记录后核对证据，只更新当前这一次执行的停止状态。
def confirm_stopped_execution(
    factory: sessionmaker[Session], evidence: StopEvidence
) -> Literal["confirmed", "already_confirmed", "rejected"]:
    """Apply trusted process evidence to its exact lifecycle rows in one short transaction."""
    if evidence.sha256 != _evidence_hash(evidence.identity, evidence.source, evidence.observation):
        return "rejected"
    identity = evidence.identity
    if identity.process_scope != PROCESS_SCOPE:
        return "rejected"
    if evidence.source == "worker_supervisor" and evidence.observation != {"session_stopped": True}:
        return "rejected"
    if evidence.source == "operator_container_exit":
        if evidence.observation.get("container_state") != "exited":
            return "rejected"
        container_id = evidence.observation.get("container_id")
        if not isinstance(container_id, str) or container_id[:12] != identity.worker_container_id[:12]:
            return "rejected"

    with factory() as session:
        video_id = session.scalar(
            select(AnalysisTask.video_id).where(AnalysisTask.id == identity.task_id)
        )
        session.commit()
    if video_id is None:
        return "rejected"

    # 先锁定原 task、attempt 和 execution 再校验身份，旧容器或旧 generation 的证据不能解锁。
    with db_transaction(factory) as session:
        video = session.scalar(select(Video).where(Video.id == video_id).with_for_update())
        task = session.scalar(
            select(AnalysisTask).where(AnalysisTask.id == identity.task_id).with_for_update()
        )
        attempt = session.scalar(
            select(AnalysisAttempt)
            .where(AnalysisAttempt.id == identity.attempt_id, AnalysisAttempt.task_id == identity.task_id)
            .with_for_update()
        )
        execution = session.scalar(
            select(AnalysisExecution)
            .where(
                AnalysisExecution.id == identity.execution_id,
                AnalysisExecution.task_id == identity.task_id,
                AnalysisExecution.attempt_id == identity.attempt_id,
            )
            .with_for_update()
        )
        if (
            video is None
            or task is None
            or attempt is None
            or execution is None
            or task.video_id != video.id
            or attempt.claim_count != 1
            or attempt.execution_id != execution.id
            or not _matches(execution, identity)
        ):
            return "rejected"
        if execution.stop_confirmed_at is not None:
            return "already_confirmed" if execution.stop_evidence_sha256 == evidence.sha256 else "rejected"
        if task.current_attempt_id != attempt.id:
            return "rejected"
        if evidence.source == "worker_supervisor" and execution.supervisor_registered_at is None:
            return "rejected"

        now = datetime.now(UTC)
        execution.stop_confirmed_at = now
        execution.stop_confirmation_source = evidence.source
        execution.stop_reason = (
            "controlled_session_empty"
            if evidence.source == "worker_supervisor"
            else "operator_container_exited"
        )
        execution.stop_evidence_sha256 = evidence.sha256
        attempt.stop_confirmed_at = now

        if attempt.status == "running":
            attempt.status = "failed"
            attempt.failure_code = attempt.failure_code or "WORKER_EXECUTION_INTERRUPTED"
            attempt.finished_at = now
        if task.status == "running":
            task.status = "failed"
            task.stage = "failed"
            task.failure_code = task.failure_code or "WORKER_EXECUTION_INTERRUPTED"
            if video.status == "ready" and video.media_status == "available":
                task.delivery_wait_reason = "automatic_recovery_pending"
                task.delivery_next_check_at = now
            else:
                task.queue_entered_at = None
                task.delivery_wait_reason = (
                    "VIDEO_MEDIA_DELETION_REQUESTED"
                    if video.status == "ready"
                    else "VIDEO_DELETION_REQUESTED"
                )
                task.delivery_next_check_at = None
            task.updated_at = now
        if attempt.status == "failed" and attempt.finished_at is None:
            attempt.finished_at = now
        if task.summary_status == "running":
            task.summary_status = "failed"
        unfinished_requests = list(
            session.scalars(
                select(SummaryRequest)
                .where(
                    SummaryRequest.execution_id == execution.id,
                    SummaryRequest.status.in_(("started", "response_received")),
                )
                .with_for_update()
            )
        )
        for request in unfinished_requests:
            request.status = "failed"
            request.error_code = "WORKER_EXECUTION_INTERRUPTED"
            request.finished_at = now
        # cleanup_state is deliberately untouched: confirmed stop and file cleanup are separate facts.
        record_analysis_event(
            session,
            task_id=task.id,
            event_key=f"stop-confirmed:{execution.id}",
            event_type="stop_confirmed",
            attempt_id=attempt.id,
            execution_id=execution.id,
            stage=task.stage,
            error_code=execution.stop_reason,
        )
    return "confirmed"


def confirm_operator_container_stop(
    factory: sessionmaker[Session],
    execution_id: uuid.UUID,
    worker_instance_id: uuid.UUID,
    container_id: str,
    *,
    container_exited: bool,
) -> Literal["confirmed", "already_confirmed", "rejected"]:
    with factory() as session:
        execution = session.scalar(select(AnalysisExecution).where(AnalysisExecution.id == execution_id))
        identity = identity_from_execution(execution) if execution is not None else None
        session.commit()
    if identity is None or identity.worker_instance_id != worker_instance_id:
        return "rejected"
    try:
        evidence = operator_container_stop_evidence(
            identity, container_id=container_id, exited=container_exited
        )
    except ValueError:
        return "rejected"
    return confirm_stopped_execution(factory, evidence)


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a verified local Stage 2 Worker container stop.")
    parser.add_argument("--execution-id", required=True, type=uuid.UUID)
    parser.add_argument("--worker-instance-id", required=True, type=uuid.UUID)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--container-exited", action="store_true")
    args = parser.parse_args()
    if not args.container_exited:
        parser.error("the operator script must verify that the exact Worker container exited")
    engine = create_database_engine(get_settings())
    try:
        result = confirm_operator_container_stop(
            create_session_factory(engine),
            args.execution_id,
            args.worker_instance_id,
            args.container_id,
            container_exited=True,
        )
    finally:
        engine.dispose()
    if result == "rejected":
        print(
            "STOP_CONFIRMATION_REQUIRED: the stopped container does not match a bound execution",
            file=sys.stderr,
        )
        return 2
    print(f"execution={args.execution_id} stop_confirmation={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
