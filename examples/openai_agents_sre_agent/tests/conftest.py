"""每个测试用一份全新的库，避免回放用例互相污染。

与 examples/langgraph_sre_agent/tests/conftest.py 同一套隔离纪律：两个框架的端到端
测试都走服务端这套存储，隔离不变量必须一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest


@pytest.fixture()
def afr_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db_path = tmp_path / "afr.db"
    monkeypatch.setenv("AFR_DB_PATH", str(db_path))
    monkeypatch.setenv("AFR_SEED_ON_STARTUP", "false")

    from afr_server import config, db

    config.get_settings.cache_clear()
    db.reset_engine()
    db.init_db()
    try:
        yield db_path
    finally:
        db.reset_engine()
        config.get_settings.cache_clear()
