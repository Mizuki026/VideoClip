from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from videoclip.models import AnalysisAttempt, AnalysisExecution, AnalysisTask, Video

# 投递协调只短暂持有事务锁；执行期间的全局占位由 Worker 会话锁负责。
EXECUTION_ADVISORY_LOCK_KEY = 0x566964656F436C69


# 事务级互斥只保护短暂的投递预留过程，函数返回后锁随事务结束。
def try_acquire_execution_gate(session: Session) -> bool:
    """Serialize a short delivery reservation with the Worker execution lease."""
    return bool(
        session.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": EXECUTION_ADVISORY_LOCK_KEY},
        ).scalar_one()
    )


# 只要存在未确认停止的执行，部署级分析槽位就仍被占用。
def find_unconfirmed_execution(session: Session) -> AnalysisExecution | None:
    """Return the deployment-wide execution that still holds the analysis slot."""
    return session.scalar(
        select(AnalysisExecution)
        .where(AnalysisExecution.stop_confirmed_at.is_(None))
        .order_by(AnalysisExecution.started_at, AnalysisExecution.id)
        .limit(1)
    )


# 候选任务按持久化入队时间和编号排序，确保多个工作进程遵守同一先进先出顺序。
def find_next_queued_task(session: Session) -> uuid.UUID | None:
    """Return the oldest currently eligible Stage-4 task by its durable queue key."""
    return session.scalar(
        select(AnalysisTask.id)
        .join(AnalysisAttempt, AnalysisAttempt.id == AnalysisTask.current_attempt_id)
        .join(Video, Video.id == AnalysisTask.video_id)
        .where(
            AnalysisTask.status == "queued",
            AnalysisTask.queue_entered_at.is_not(None),
            AnalysisAttempt.status == "queued",
            AnalysisAttempt.claim_count == 0,
            AnalysisAttempt.execution_id.is_(None),
            Video.status == "ready",
            Video.current_analysis_task_id == AnalysisTask.id,
            ~select(AnalysisExecution.id)
            .where(AnalysisExecution.attempt_id == AnalysisAttempt.id)
            .exists(),
        )
        .order_by(AnalysisTask.queue_entered_at, AnalysisTask.id)
        .limit(1)
    )
