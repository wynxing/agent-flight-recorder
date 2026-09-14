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

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)


#: 新增可空列。仓库刻意不引入迁移框架（SQLite 单文件、本地单用户），
#: 因此这里只做最小、可重入的补齐：create_all 不会给已存在的表加列。
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    # 用例「最近一次结论」的定义前提：结论是在哪一版定义下得出的。
    "cases": {
        "last_cause": "JSON",
        "last_condition": "JSON",
        "last_definition_digest": "TEXT",
    },
    # 整批预算：声明的上限与整批的记账都是后加的字段，旧库要能直接补上。
    # case_set_version 同理：套件所属的用例集版本（见 case_versions.py）。补出来的列在
    # 存量行上是 NULL，读出来就是「无版本记录」——版本化之前建的批次确实没有版本。
    "suites": {"budget": "JSON", "budget_usage": "JSON", "case_set_version": "TEXT"},
    # 格子同样要能说清自己归属哪一版（格子才是被执行的那个单位）。
    "suite_items": {"case_set_version": "TEXT"},
}


def _add_missing_columns(engine: Engine) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in _ADDITIVE_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl_type in columns.items():
                if name in present:
                    continue
                connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl_type}'))


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


def bound_db_path() -> str | None:
    """此刻 engine 绑定的库文件；engine 还没建、或者已被重置时返回 None。

    给日志与诊断用，**不建 engine**（诊断不该有副作用）。后台线程连错库的典型症状是
    `no such table`，而那句话不带库名时几乎无法定位：issue #18 的 CI 日志里就只有一个
    表名。写入失败的地方顺带把库名说清楚，下一次就能一眼看出线程跑到了哪个库上。
    """

    return None if _engine is None else str(_engine.url.database)
