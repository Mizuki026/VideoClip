from __future__ import annotations

from typing import Any

import pytest

from videoclip import maintenance_worker
from videoclip.celery_app import celery_app


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.closed = False

    def set(self, key: str, value: str, nx: bool = False) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

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

    def execute(self, statement: Any, _parameters: dict[str, int]) -> FakeScalar:
        self.statements.append(str(statement))
        return FakeScalar(self.acquired)


def test_maintenance_route_and_periodic_schedule_are_isolated() -> None:
    assert celery_app.conf.task_default_queue == "analysis"
    assert celery_app.conf.task_routes["videoclip.transcribe_analysis"]["queue"] == "analysis"
    maintenance_route = celery_app.conf.task_routes["videoclip.maintenance.reclaim_expired_uploads"]
    assert maintenance_route["queue"] == "maintenance"
    schedule = celery_app.conf.beat_schedule["reclaim-expired-resumable-uploads"]
    assert schedule["task"] == "videoclip.maintenance.reclaim_expired_uploads"
    assert schedule["options"]["queue"] == "maintenance"


def test_schedule_lock_coalesces_duplicate_ticks_until_the_task_releases_it() -> None:
    redis = FakeRedis()
    assert maintenance_worker._claim_schedule_lock("tick-1", redis)
    assert not maintenance_worker._claim_schedule_lock("tick-2", redis)
    assert not maintenance_worker._release_schedule_lock("tick-2", redis)
    assert maintenance_worker._release_schedule_lock("tick-1", redis)
    assert maintenance_worker._claim_schedule_lock("tick-3", redis)


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
