from __future__ import annotations

import os

from celery import Celery  # type: ignore[import-untyped]
from kombu import Queue  # type: ignore[import-untyped]

BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
RECLAIM_INTERVAL_SECONDS = int(os.environ.get("UPLOAD_RECLAIM_INTERVAL_SECONDS", "60"))
if not 60 <= RECLAIM_INTERVAL_SECONDS <= 3600:
    raise RuntimeError("UPLOAD_RECLAIM_INTERVAL_SECONDS must be between 60 and 3600")

celery_app = Celery(
    "videoclip",
    broker=BROKER_URL,
    include=["videoclip.analysis_worker", "videoclip.maintenance_worker"],
)
celery_app.conf.update(
    task_default_queue="analysis",
    task_default_exchange="analysis",
    task_default_routing_key="analysis.transcribe",
    task_queues=(
        Queue("analysis", exchange="analysis", routing_key="analysis.transcribe"),
        Queue("maintenance", exchange="maintenance", routing_key="maintenance.reclaim"),
    ),
    task_routes={
        "videoclip.transcribe_analysis": {"queue": "analysis", "routing_key": "analysis.transcribe"},
        "videoclip.maintenance.reclaim_expired_uploads": {
            "queue": "maintenance",
            "routing_key": "maintenance.reclaim",
        },
    },
    task_serializer="json",
    accept_content=["json"],
    result_backend=None,
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    task_acks_late=False,
    task_reject_on_worker_lost=False,
    task_publish_retry=False,
    worker_prefetch_multiplier=1,
    worker_concurrency=1,
    worker_enable_remote_control=False,
    task_track_started=False,
    task_soft_time_limit=10_830,
    task_time_limit=10_860,
    beat_schedule={
        "reclaim-expired-resumable-uploads": {
            "task": "videoclip.maintenance.reclaim_expired_uploads",
            "schedule": float(RECLAIM_INTERVAL_SECONDS),
            "options": {"queue": "maintenance", "routing_key": "maintenance.reclaim"},
        },
    },
    broker_connection_retry_on_startup=False,
)
