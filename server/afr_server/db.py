"""数据库连接与初始化。SQLite 单文件，无迁移框架：表结构由 SQLModel 直接建立。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.db_url,
            echo=False,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _record):  # pragma: no cover - 驱动回调
            cursor = dbapi_connection.cursor()
            # WAL 让"边录制边读时间线"不会互相阻塞。
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return _engine


def init_db() -> None:
    from . import tables  # noqa: F401 - 触发模型注册

    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """测试用：丢掉缓存的 engine。"""

    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None

