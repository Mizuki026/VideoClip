from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from videoclip.settings import Settings


# 连接池在这里统一设置预检、等待上限和语句超时，避免请求线程无限等待数据库。
def create_database_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
        pool_timeout=5,
        connect_args={"options": "-c lock_timeout=5000 -c statement_timeout=30000"},
    )


# 会话工厂关闭自动刷新并保留提交后的对象值，事务边界由调用方明确控制。
def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        # 同一事务内的写入一起提交；代码抛出异常时由事务上下文回滚。
        with session.begin():
            yield session


# 启动检查只执行轻量查询，用于确认数据库连接确实可用。
def check_database(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
