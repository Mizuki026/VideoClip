from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from celery import Task  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from videoclip.celery_app import BROKER_URL, celery_app
from videoclip.db import create_database_engine, create_session_factory
from videoclip.resumable_uploads import reclaim_expired_uploads
from videoclip.settings import get_settings
from videoclip.storage import MediaStore

logger = logging.getLogger("videoclip.maintenance")
RECLAIM_LOCK_KEY = "videoclip:maintenance:reclaim-expired:scheduled"
RECLAIM_ADVISORY_LOCK_ID = 73_102_026_100_003
DEFAULT_SCAN_LIMIT = 4
MAX_SCAN_LIMIT = 8


def _claim_schedule_lock(token: str, client: Any | None = None) -> bool:
    redis_client = client or Redis.from_url(BROKER_URL, decode_responses=True)
    try:
        return bool(redis_client.set(RECLAIM_LOCK_KEY, token, nx=True))
    finally:
        if client is None:
            redis_client.close()


def _configured_scan_limit() -> int:
    raw = os.environ.get("UPLOAD_RECLAIM_BATCH_SIZE", str(DEFAULT_SCAN_LIMIT))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("UPLOAD_RECLAIM_BATCH_SIZE must be an integer") from exc
    if not 1 <= value <= MAX_SCAN_LIMIT:
        raise RuntimeError(f"UPLOAD_RECLAIM_BATCH_SIZE must be between 1 and {MAX_SCAN_LIMIT}")
    return value


def _release_schedule_lock(token: str, client: Any | None = None) -> bool:
    redis_client = client or Redis.from_url(BROKER_URL, decode_responses=True)
    try:
        current = redis_client.get(RECLAIM_LOCK_KEY)
        if current != token:
            return False
        return bool(redis_client.delete(RECLAIM_LOCK_KEY))
    finally:
        if client is None:
            redis_client.close()


class SingleFlightReclaimTask(Task):  # type: ignore[misc]
    """Keep at most one scheduled reclaim task queued or running in Redis."""

    abstract = True

    def apply_async(self, args: Any = None, kwargs: Any = None, **options: Any) -> Any:
        task_id = str(options.get("task_id") or uuid.uuid4())
        redis_client = Redis.from_url(BROKER_URL, decode_responses=True)
        try:
            if not _claim_schedule_lock(task_id, redis_client):
                logger.info("reclaim_schedule_coalesced")
                return None
            task_kwargs = dict(kwargs or {})
            task_kwargs["_schedule_lock_token"] = task_id
            options["task_id"] = task_id
            try:
                return super().apply_async(args=args, kwargs=task_kwargs, **options)
            except Exception:
                _release_schedule_lock(task_id, redis_client)
                raise
        except Exception:
            logger.warning("reclaim_schedule_publish_unavailable")
            return None
        finally:
            redis_client.close()  # type: ignore[no-untyped-call]

    def after_return(
        self,
        status: str,
        retval: Any,
        task_id: str,
        args: Any,
        kwargs: Any,
        einfo: Any,
    ) -> None:
        token = kwargs.get("_schedule_lock_token") if isinstance(kwargs, dict) else None
        if isinstance(token, str):
            try:
                _release_schedule_lock(token)
            except Exception:
                logger.warning("reclaim_schedule_lock_release_failed")
        super().after_return(status, retval, task_id, args, kwargs, einfo)


def _reclaim_with_database_lock(
    factory: sessionmaker[Session], store: MediaStore, connection: Connection, limit: int
) -> dict[str, int | str]:
    acquired = bool(connection.execute(
        text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": RECLAIM_ADVISORY_LOCK_ID}
    ).scalar_one())
    if not acquired:
        return {"status": "overlap_skipped", "reclaimed": 0}
    try:
        count = reclaim_expired_uploads(factory, store, limit=limit)
        return {"status": "completed", "reclaimed": count}
    finally:
        connection.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": RECLAIM_ADVISORY_LOCK_ID}
        )


@celery_app.task(  # type: ignore[misc]
    name="videoclip.maintenance.reclaim_expired_uploads",
    base=SingleFlightReclaimTask,
    queue="maintenance",
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
    soft_time_limit=140,
    time_limit=150,
)
def reclaim_expired_uploads_task(_schedule_lock_token: str | None = None) -> dict[str, int | str]:
    del _schedule_lock_token
    settings = get_settings()
    engine = create_database_engine(settings)
    store = MediaStore(settings.app_data_root)
    try:
        store.prepare()
        with engine.connect() as connection:
            result = _reclaim_with_database_lock(
                create_session_factory(engine), store, connection, _configured_scan_limit()
            )
        logger.info(
            "reclaim_cycle_finished status=%s reclaimed=%s",
            result["status"],
            result["reclaimed"],
        )
        return result
    except Exception:
        logger.exception("reclaim_cycle_failed")
        raise
    finally:
        engine.dispose()
