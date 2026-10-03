from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from videoclip.db import create_database_engine, create_session_factory
from videoclip.settings import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]
TEST_DB_NAME = os.environ.get("VIDEOCLIP_TEST_DB_NAME", "videoclip_mvp_stage1_test")
TEST_DATA = Path(
    os.environ.get("VIDEOCLIP_TEST_DATA_DIR", str(ROOT / "data" / "mvp-stage-1" / "test-data"))
)
os.environ["APP_ENV"] = "test"
os.environ["ANALYSIS_TEST_INLINE_SUPERVISOR"] = "1"
os.environ["DB_NAME"] = TEST_DB_NAME
os.environ["APP_DATA_ROOT"] = str(TEST_DATA / "app")
get_settings.cache_clear()
TEST_SETTINGS = Settings()


def pytest_sessionstart(session: pytest.Session) -> None:
    _ = session
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    return TEST_SETTINGS


@pytest.fixture(scope="session")
def test_engine(test_settings: Settings) -> Engine:
    engine = create_database_engine(test_settings)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def session_factory(test_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(test_engine)


@pytest.fixture(autouse=True)
def empty_database(session_factory: sessionmaker[Session]) -> None:
    if TEST_SETTINGS.db_name != TEST_DB_NAME:
        raise AssertionError("integration tests must only use the configured isolated test database")
    with session_factory.begin() as session:
        session.execute(
            text(
                "TRUNCATE TABLE summary_requests, analysis_summaries, analysis_executions, "
                "transcript_segments, task_deliveries, "
                "analysis_attempts, analysis_tasks, cleanup_intents, videos, upload_chunks, upload_handoffs, "
                "sessions, users "
                "RESTART IDENTITY CASCADE"
            )
        )
