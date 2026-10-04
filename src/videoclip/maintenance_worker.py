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

from videoclip.analysis_delivery import DeliveryPolicy, coordinate_due_analysis_deliveries
from videoclip.analysis_lifecycle import coordinate_due_analysis_recoveries
from videoclip.celery_app import BROKER_URL, celery_app
from videoclip.db import create_database_engine, create_session_factory
from videoclip.resumable_uploads import reclaim_expired_uploads
from videoclip.settings import Settings, get_settings
from videoclip.storage import MediaStore

logger = logging.getLogger("videoclip.maintenance")
RECLAIM_LOCK_KEY = "videoclip:maintenance:reclaim-expired:scheduled"
DELIVERY_LOCK_KEY = "videoclip:maintenance:coordinate-analysis:scheduled"
SCHEDULE_LOCK_TTL_SECONDS = 180
RECLAIM_ADVISORY_LOCK_ID = 73_102_026_100_003
DELIVERY_ADVISORY_LOCK_ID = 73_102_026_100_004
DEFAULT_SCAN_LIMIT = 4
MAX_SCAN_LIMIT = 8


def _claim_schedule_lock(token: str, client: Any | None = None) -> bool:
    redis_client = client or Redis.from_url(BROKER_URL, decode_responses=True)
    try:
        return bool(redis_client.set(RECLAIM_LOCK_KEY, token, nx=True, ex=SCHEDULE_LOCK_TTL_SECONDS))
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
        return bool(
            redis_client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                RECLAIM_LOCK_KEY,
                token,
            )
        )
    finally:
        if client is None:
            redis_client.close()


class SingleFlightMaintenanceTask(Task):  # type: ignore[misc]
    """Coalesce scheduled work with an expiring, owner-checked Redis lock."""

    abstract = True
    lock_key = RECLAIM_LOCK_KEY

    def _claim(self, token: str, client: Any) -> bool:
        return bool(client.set(self.lock_key, token, nx=True, ex=SCHEDULE_LOCK_TTL_SECONDS))

    def _release(self, token: str, client: Any) -> bool:
        return bool(
            client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                self.lock_key,
                token,
            )
        )

    def apply_async(self, args: Any = None, kwargs: Any = None, **options: Any) -> Any:
        task_id = str(options.get("task_id") or uuid.uuid4())
        redis_client = Redis.from_url(BROKER_URL, decode_responses=True)
        try:
            if not self._claim(task_id, redis_client):
                logger.info("maintenance_schedule_coalesced task=%s", self.name)
                return None
            task_kwargs = dict(kwargs or {})
            task_kwargs["_schedule_lock_token"] = task_id
            options["task_id"] = task_id
            try:
                return super().apply_async(args=args, kwargs=task_kwargs, **options)
            except Exception:
                self._release(task_id, redis_client)
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
            redis_client = Redis.from_url(BROKER_URL, decode_responses=True)
            try:
                self._release(token, redis_client)
            except Exception:
                logger.warning("maintenance_schedule_lock_release_failed task=%s", self.name)
            finally:
                redis_client.close()  # type: ignore[no-untyped-call]
        super().after_return(status, retval, task_id, args, kwargs, einfo)


class SingleFlightReclaimTask(SingleFlightMaintenanceTask):
    lock_key = RECLAIM_LOCK_KEY


class SingleFlightDeliveryTask(SingleFlightMaintenanceTask):
    lock_key = DELIVERY_LOCK_KEY


def _reclaim_with_database_lock(
    factory: sessionmaker[Session], store: MediaStore, connection: Connection, limit: int
) -> dict[str, int | str]:
    acquired = bool(
        connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": RECLAIM_ADVISORY_LOCK_ID}
        ).scalar_one()
    )
    if not acquired:
        return {"status": "overlap_skipped", "reclaimed": 0}
    try:
        count = reclaim_expired_uploads(factory, store, limit=limit)
        return {"status": "completed", "reclaimed": count}
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": RECLAIM_ADVISORY_LOCK_ID})


def _coordinate_with_database_lock(
    factory: sessionmaker[Session],
    connection: Connection,
    policy: DeliveryPolicy,
    settings: Settings | None = None,
) -> dict[str, int | str]:
    acquired = bool(
        connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": DELIVERY_ADVISORY_LOCK_ID}
        ).scalar_one()
    )
    if not acquired:
        return {"status": "overlap_skipped", "scanned": 0, "published": 0, "blocked": 0, "exhausted": 0}
    try:
        recovery = (
            coordinate_due_analysis_recoveries(
                factory,
                settings=settings,
                delivery_policy=policy,
                limit=min(4, policy.scan_limit),
            )
            if settings is not None
            else None
        )
        result = coordinate_due_analysis_deliveries(
            factory,
            lambda task_id, attempt_id: celery_app.send_task(
                "videoclip.transcribe_analysis", args=[task_id, attempt_id], retry=False
            ),
            policy=policy,
        )
        result_values: dict[str, int | str] = {
            "status": "completed",
            "scanned": result.scanned,
            "published": result.published,
            "blocked": result.blocked,
            "exhausted": result.exhausted,
        }
        if recovery is not None:
            result_values.update(
                {
                    "recovery_scanned": recovery.scanned,
                    "recovery_resumed": recovery.resumed,
                    "recovery_waiting": recovery.waiting,
                    "recovery_blocked": recovery.blocked,
                    "recovery_exhausted": recovery.exhausted,
                }
            )
        return result_values
    finally:
        connection.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": DELIVERY_ADVISORY_LOCK_ID}
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


@celery_app.task(  # type: ignore[misc]
    name="videoclip.maintenance.coordinate_analysis_deliveries",
    base=SingleFlightDeliveryTask,
    queue="maintenance",
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
    soft_time_limit=140,
    time_limit=150,
)
def coordinate_analysis_deliveries_task(_schedule_lock_token: str | None = None) -> dict[str, int | str]:
    del _schedule_lock_token
    settings = get_settings()
    engine = create_database_engine(settings)
    policy = DeliveryPolicy.from_settings(settings)
    try:
        with engine.connect() as connection:
            result = _coordinate_with_database_lock(
                create_session_factory(engine), connection, policy, settings
            )
        logger.info(
            "analysis_delivery_cycle_finished status=%s scanned=%s published=%s blocked=%s exhausted=%s "
            "recovery_scanned=%s recovery_resumed=%s recovery_waiting=%s recovery_blocked=%s "
            "recovery_exhausted=%s",
            result["status"],
            result["scanned"],
            result["published"],
            result["blocked"],
            result["exhausted"],
            result.get("recovery_scanned", 0),
            result.get("recovery_resumed", 0),
            result.get("recovery_waiting", 0),
            result.get("recovery_blocked", 0),
            result.get("recovery_exhausted", 0),
        )
        return result
    except Exception:
        logger.exception("analysis_delivery_cycle_failed")
        raise
    finally:
        engine.dispose()
