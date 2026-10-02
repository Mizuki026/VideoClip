from __future__ import annotations

import os

from celery import Celery  # type: ignore[import-untyped]

BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")

celery_app = Celery("videoclip", broker=BROKER_URL, include=["videoclip.analysis_worker"])
celery_app.conf.update(
    task_default_queue="analysis",
    task_default_exchange="analysis",
    task_default_routing_key="analysis.transcribe",
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
    broker_connection_retry_on_startup=False,
)
