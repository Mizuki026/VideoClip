from __future__ import annotations

import argparse
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from videoclip.models import (
    AnalysisAttempt,
    AnalysisExecution,
    AnalysisSummary,
    AnalysisTask,
    SummaryRequest,
    TaskDelivery,
    TranscriptSegment,
    User,
    Video,
)
from videoclip.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "data" / "mvp-stage-2" / ".env"
USERNAME_PATTERN = re.compile(r"stage5fixture-[0-9a-f]{12}")


def _engine() -> Engine:
    return create_engine(Settings(_env_file=ENV_FILE).database_url())  # type: ignore[call-arg]


def seed_fixture(video_id: uuid.UUID, task_id: uuid.UUID, attempt_id: uuid.UUID) -> None:
    engine = _engine()
    try:
        with Session(engine) as session, session.begin():
            now = datetime.now(UTC)
            video = session.scalar(select(Video).where(Video.id == video_id).with_for_update())
            task = session.scalar(select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update())
            attempt = session.scalar(
                select(AnalysisAttempt).where(AnalysisAttempt.id == attempt_id).with_for_update()
            )
            if video is None or task is None or attempt is None:
                raise SystemExit("Fixture rejected: requested rows do not exist.")
            user = session.get(User, video.owner_id)
            if user is None or USERNAME_PATTERN.fullmatch(user.username) is None:
                raise SystemExit("Fixture rejected: owner is not a generated Stage 5 test account.")
            if user.created_at < now - timedelta(hours=2):
                raise SystemExit("Fixture rejected: generated account is outside the test time window.")
            if task.video_id != video.id or task.owner_id != user.id or task.current_attempt_id != attempt.id:
                raise SystemExit("Fixture rejected: task, video, owner, or current attempt do not match.")
            if video.status != "ready" or task.status != "queued" or attempt.status != "queued":
                raise SystemExit("Fixture rejected: task is not a newly queued analysis.")
            if attempt.claim_count != 0 or attempt.execution_id is not None:
                raise SystemExit("Fixture rejected: analysis attempt has already been claimed.")
            if session.scalar(select(AnalysisExecution.id).where(AnalysisExecution.attempt_id == attempt.id)):
                raise SystemExit("Fixture rejected: an execution row already exists.")
            if session.scalar(
                select(AnalysisAttempt.id).where(AnalysisAttempt.parent_attempt_id == attempt.id)
            ):
                raise SystemExit("Fixture rejected: recovery lineage already exists.")
            delivery = session.get(TaskDelivery, attempt.id)
            if delivery is None or delivery.status not in {"pending", "sent"}:
                raise SystemExit("Fixture rejected: task delivery is not in its initial state.")
            if session.scalar(select(TranscriptSegment.id).where(TranscriptSegment.attempt_id == attempt.id)):
                raise SystemExit("Fixture rejected: transcript rows already exist.")
            if session.scalar(select(AnalysisSummary.id).where(AnalysisSummary.task_id == task.id)):
                raise SystemExit("Fixture rejected: a summary already exists.")
            if session.scalar(select(SummaryRequest.id).where(SummaryRequest.task_id == task.id)):
                raise SystemExit("Fixture rejected: model requests already exist.")
            unconfirmed = session.scalar(
                select(func.count())
                .select_from(AnalysisExecution)
                .where(AnalysisExecution.stop_confirmed_at.is_(None))
            )
            if unconfirmed:
                raise SystemExit("Fixture rejected: another execution has not confirmed stop.")
            attempt.status = "failed"
            attempt.failure_code = "BROKER_PAYLOAD_REJECTED"
            attempt.finished_at = now
            attempt.cleanup_state = "failed"
            attempt.cleanup_error_code = "ATTEMPT_TEMP_CLEANUP_FAILED"
            task.status = "failed"
            task.stage = "failed"
            task.failure_code = "BROKER_PAYLOAD_REJECTED"
            delivery.status = "failed"
            delivery.error_code = "BROKER_PAYLOAD_REJECTED"
            delivery.confirmed_at = None
            delivery.updated_at = now
    finally:
        engine.dispose()
    print("fixture=seeded")


def verify_recovery(task_id: uuid.UUID, parent_attempt_id: uuid.UUID, current_attempt_id: uuid.UUID) -> None:
    engine = _engine()
    try:
        with Session(engine) as session:
            task = session.get(AnalysisTask, task_id)
            attempts = list(
                session.scalars(select(AnalysisAttempt).where(AnalysisAttempt.task_id == task_id))
            )
            child = session.get(AnalysisAttempt, current_attempt_id)
            summary_requests = session.scalar(
                select(func.count()).select_from(SummaryRequest).where(SummaryRequest.task_id == task_id)
            )
            transcript_segments = (
                session.scalar(
                    select(func.count())
                    .select_from(TranscriptSegment)
                    .where(TranscriptSegment.attempt_id.in_([attempt.id for attempt in attempts]))
                )
                if attempts
                else 0
            )
            executions = session.scalar(
                select(func.count())
                .select_from(AnalysisExecution)
                .where(AnalysisExecution.task_id == task_id)
            )
            if (
                task is None
                or task.current_attempt_id != current_attempt_id
                or len(attempts) != 2
                or child is None
                or child.parent_attempt_id != parent_attempt_id
                or child.status != "queued"
                or summary_requests != 0
                or transcript_segments != 0
                or executions != 0
            ):
                raise SystemExit("Fixture verification failed: recovery changed an unexpected task fact.")
    finally:
        engine.dispose()
    print("attempts=2; transcript_segments=0; summary_requests=0; executions=0")


def prepare_delete(video_id: uuid.UUID, task_id: uuid.UUID, attempt_id: uuid.UUID) -> None:
    engine = _engine()
    try:
        with Session(engine) as session, session.begin():
            now = datetime.now(UTC)
            video = session.scalar(select(Video).where(Video.id == video_id).with_for_update())
            task = session.scalar(select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update())
            attempt = session.scalar(
                select(AnalysisAttempt).where(AnalysisAttempt.id == attempt_id).with_for_update()
            )
            if video is None or task is None or attempt is None:
                raise SystemExit("Fixture delete preparation rejected: rows do not exist.")
            user = session.get(User, video.owner_id)
            if user is None or USERNAME_PATTERN.fullmatch(user.username) is None:
                raise SystemExit("Fixture delete preparation rejected: owner is not a generated account.")
            if task.video_id != video.id or task.owner_id != user.id or task.current_attempt_id != attempt.id:
                raise SystemExit(
                    "Fixture delete preparation rejected: task and current attempt do not match."
                )
            if video.status != "ready" or task.status != "queued" or attempt.status != "queued":
                raise SystemExit("Fixture delete preparation rejected: task is not newly queued.")
            if attempt.claim_count != 0 or attempt.execution_id is not None:
                raise SystemExit("Fixture delete preparation rejected: attempt is already claimed.")
            if session.scalar(select(AnalysisExecution.id).where(AnalysisExecution.attempt_id == attempt.id)):
                raise SystemExit("Fixture delete preparation rejected: an execution row exists.")
            delivery = session.get(TaskDelivery, attempt.id)
            if delivery is None or delivery.status not in {"pending", "sent"}:
                raise SystemExit("Fixture delete preparation rejected: delivery state is unexpected.")
            unconfirmed = session.scalar(
                select(func.count())
                .select_from(AnalysisExecution)
                .where(AnalysisExecution.stop_confirmed_at.is_(None))
            )
            if unconfirmed:
                raise SystemExit("Fixture delete preparation rejected: another execution is unconfirmed.")
            attempt.status = "failed"
            attempt.failure_code = "STAGE5_FIXTURE_CLEANUP"
            attempt.finished_at = now
            task.status = "failed"
            task.stage = "failed"
            task.failure_code = "STAGE5_FIXTURE_CLEANUP"
            delivery.status = "failed"
            delivery.error_code = "STAGE5_FIXTURE_CLEANUP"
            delivery.confirmed_at = None
            delivery.updated_at = now
    finally:
        engine.dispose()
    print("fixture_delete_ready=1")


def remove_empty_account(username: str) -> None:
    if USERNAME_PATTERN.fullmatch(username) is None:
        raise SystemExit("Cleanup rejected: owner is not a generated Stage 5 test account.")
    engine = _engine()
    try:
        with Session(engine) as session, session.begin():
            user = session.scalar(select(User).where(User.username == username).with_for_update())
            if user is None:
                return
            if user.created_at < datetime.now(UTC) - timedelta(hours=2):
                raise SystemExit("Cleanup rejected: generated account is outside the test time window.")
            if session.scalar(select(Video.id).where(Video.owner_id == user.id)):
                raise SystemExit("Cleanup rejected: generated account still owns videos.")
            if session.scalar(select(AnalysisTask.id).where(AnalysisTask.owner_id == user.id)):
                raise SystemExit("Cleanup rejected: generated account still owns tasks.")
            session.delete(user)
    finally:
        engine.dispose()
    print("empty_generated_account_removed=1")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or verify a bounded Stage 5 recovery test fixture.")
    parser.add_argument("mode", choices=("seed", "verify", "prepare-delete", "cleanup-account"))
    parser.add_argument("--video-id", type=uuid.UUID)
    parser.add_argument("--task-id", type=uuid.UUID)
    parser.add_argument("--attempt-id", type=uuid.UUID)
    parser.add_argument("--parent-attempt-id", type=uuid.UUID)
    parser.add_argument("--current-attempt-id", type=uuid.UUID)
    parser.add_argument("--username")
    arguments = parser.parse_args()
    if arguments.mode == "seed":
        if not (arguments.video_id and arguments.task_id and arguments.attempt_id):
            parser.error("seed requires --video-id, --task-id, and --attempt-id")
        seed_fixture(arguments.video_id, arguments.task_id, arguments.attempt_id)
    elif arguments.mode == "verify":
        if not (arguments.task_id and arguments.parent_attempt_id and arguments.current_attempt_id):
            parser.error("verify requires --task-id, --parent-attempt-id, and --current-attempt-id")
        verify_recovery(arguments.task_id, arguments.parent_attempt_id, arguments.current_attempt_id)
    elif arguments.mode == "prepare-delete":
        if not (arguments.video_id and arguments.task_id and arguments.current_attempt_id):
            parser.error("prepare-delete requires --video-id, --task-id, and --current-attempt-id")
        prepare_delete(arguments.video_id, arguments.task_id, arguments.current_attempt_id)
    else:
        if not arguments.username:
            parser.error("cleanup-account requires --username")
        remove_empty_account(arguments.username)


if __name__ == "__main__":
    main()
