from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from videoclip import maintenance_worker
from videoclip.celery_app import celery_app


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.closed = False

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    def eval(self, _script: str, _key_count: int, key: str, token: str) -> int:
        if self.values.get(key) != token:
            return 0
        self.values.pop(key)
        return 1

    def close(self) -> None:
        self.closed = True


class FakeScalar:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar_one(self) -> bool:
        return self.value


class FakeConnection:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.statements: list[str] = []
        self.parameters: list[dict[str, int]] = []

    def execute(self, statement: Any, _parameters: dict[str, int]) -> FakeScalar:
        self.statements.append(str(statement))
        self.parameters.append(_parameters)
        return FakeScalar(self.acquired)


def test_maintenance_route_and_periodic_schedule_are_isolated() -> None:
    assert celery_app.conf.task_default_queue == "analysis"
    assert celery_app.conf.task_routes["videoclip.transcribe_analysis"]["queue"] == "analysis"
    maintenance_route = celery_app.conf.task_routes["videoclip.maintenance.reclaim_expired_uploads"]
    assert maintenance_route["queue"] == "maintenance"
    schedule = celery_app.conf.beat_schedule["reclaim-expired-resumable-uploads"]
    assert schedule["task"] == "videoclip.maintenance.reclaim_expired_uploads"
    assert schedule["options"]["queue"] == "maintenance"
    delivery_route = celery_app.conf.task_routes["videoclip.maintenance.coordinate_analysis_deliveries"]
    assert delivery_route["queue"] == "maintenance"
    delivery_schedule = celery_app.conf.beat_schedule["coordinate-analysis-deliveries"]
    assert delivery_schedule["task"] == "videoclip.maintenance.coordinate_analysis_deliveries"
    assert delivery_schedule["options"]["queue"] == "maintenance"


def test_schedule_lock_coalesces_duplicate_ticks_until_the_task_releases_it() -> None:
    redis = FakeRedis()
    assert maintenance_worker._claim_schedule_lock("tick-1", redis)
    assert redis.expirations[maintenance_worker.RECLAIM_LOCK_KEY] == 180
    assert not maintenance_worker._claim_schedule_lock("tick-2", redis)
    assert not maintenance_worker._release_schedule_lock("tick-2", redis)
    assert maintenance_worker._release_schedule_lock("tick-1", redis)
    assert maintenance_worker._claim_schedule_lock("tick-3", redis)
    redis.values[maintenance_worker.RECLAIM_LOCK_KEY] = "new-owner"
    assert not maintenance_worker._release_schedule_lock("tick-3", redis)
    assert redis.values[maintenance_worker.RECLAIM_LOCK_KEY] == "new-owner"


def test_delivery_scan_has_an_independent_expiring_lock_and_database_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery_task = maintenance_worker.SingleFlightDeliveryTask()
    redis = FakeRedis()
    assert delivery_task.lock_key == maintenance_worker.DELIVERY_LOCK_KEY
    assert delivery_task.lock_key != maintenance_worker.RECLAIM_LOCK_KEY
    assert delivery_task._claim("delivery-tick-1", redis)
    assert redis.expirations[delivery_task.lock_key] == maintenance_worker.SCHEDULE_LOCK_TTL_SECONDS
    assert not delivery_task._claim("delivery-tick-2", redis)
    assert delivery_task._release("delivery-tick-1", redis)

    calls: list[object] = []

    def scan(factory: Any, _publisher: Any, policy: Any) -> SimpleNamespace:
        calls.append((factory, policy))
        return SimpleNamespace(scanned=3, published=2, blocked=1, exhausted=0)

    monkeypatch.setattr(maintenance_worker, "coordinate_due_analysis_deliveries", scan)
    policy = maintenance_worker.DeliveryPolicy()
    locked = FakeConnection(acquired=False)
    skipped = maintenance_worker._coordinate_with_database_lock(
        None, locked, policy  # type: ignore[arg-type]
    )
    assert skipped == {
        "status": "overlap_skipped",
        "scanned": 0,
        "published": 0,
        "blocked": 0,
        "exhausted": 0,
    }
    assert calls == []
    assert locked.parameters == [{"lock_id": maintenance_worker.DELIVERY_ADVISORY_LOCK_ID}]
    assert "pg_advisory_unlock" not in " ".join(locked.statements)

    acquired = FakeConnection(acquired=True)
    completed = maintenance_worker._coordinate_with_database_lock(
        None, acquired, policy  # type: ignore[arg-type]
    )
    assert completed == {
        "status": "completed",
        "scanned": 3,
        "published": 2,
        "blocked": 1,
        "exhausted": 0,
    }
    assert len(calls) == 1 and calls[0][1] is policy  # type: ignore[index]
    assert acquired.parameters == [
        {"lock_id": maintenance_worker.DELIVERY_ADVISORY_LOCK_ID},
        {"lock_id": maintenance_worker.DELIVERY_ADVISORY_LOCK_ID},
    ]
    assert "pg_advisory_unlock" in " ".join(acquired.statements)


def test_reclaim_batch_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_RECLAIM_BATCH_SIZE", "8")
    assert maintenance_worker._configured_scan_limit() == 8
    monkeypatch.setenv("UPLOAD_RECLAIM_BATCH_SIZE", "9")
    with pytest.raises(RuntimeError, match="between 1 and 8"):
        maintenance_worker._configured_scan_limit()


def test_database_lock_skips_overlap_and_releases_after_business_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def reclaim(_factory: Any, _store: Any, *, limit: int) -> int:
        calls.append(limit)
        return 3

    monkeypatch.setattr(maintenance_worker, "reclaim_expired_uploads", reclaim)
    locked = FakeConnection(acquired=False)
    skipped = maintenance_worker._reclaim_with_database_lock(None, None, locked, 4)  # type: ignore[arg-type]
    assert skipped == {"status": "overlap_skipped", "reclaimed": 0}
    assert calls == []
    assert "pg_advisory_unlock" not in " ".join(locked.statements)

    acquired = FakeConnection(acquired=True)
    result = maintenance_worker._reclaim_with_database_lock(None, None, acquired, 4)  # type: ignore[arg-type]
    assert result == {"status": "completed", "reclaimed": 3}
    assert calls == [4]
    assert "pg_advisory_unlock" in " ".join(acquired.statements)
