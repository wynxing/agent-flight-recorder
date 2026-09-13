"""每个测试用一份全新的 SQLite 库，避免用例之间互相污染。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest


@pytest.fixture()
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db_path = tmp_path / "afr-test.db"
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


@pytest.fixture()
def client(app_env: Path):
    from fastapi.testclient import TestClient

    from afr_server.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def seeded_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """一个真的会播种的客户端。

    app_env 默认关掉播种（否则每个测试都要等一次 seed），这个 fixture 显式打开：需要
    「一条真实的父 Run + 一个已注册的 Agent」的端到端测试都用它，免得每个测试模块各自
    维护一份播种样板。
    """

    monkeypatch.setenv("AFR_DB_PATH", str(tmp_path / "afr-seed.db"))
    monkeypatch.setenv("AFR_SEED_ON_STARTUP", "true")

    from afr_server import config, db

    config.get_settings.cache_clear()
    db.reset_engine()
    db.init_db()

    from afr_server.main import app
    from fastapi.testclient import TestClient

    try:
        with TestClient(app) as client:
            yield client
    finally:
        db.reset_engine()
        config.get_settings.cache_clear()


@pytest.fixture()
def make_run():
    """以 fixture 形式提供载荷构造器。

    刻意不做成可 import 的模块：仓库里有多个 tests 目录，跨目录 import 会撞名字。
    """

    return make_run_payload


def make_run_payload(run_id: str = "run-1", **run_overrides) -> dict:
    """构造一次入库请求。多余的关键字参数会合并进 run 头。"""

    payload = {
        "protocol_version": 1,
        "sdk": {"name": "pytest", "version": "0"},
        "run": {
            "id": run_id,
            "agent_name": "demo-agent",
            "status": "succeeded",
            "started_at": "2026-09-13T00:00:00Z",
            "ended_at": "2026-09-13T00:00:05Z",
            "labels": {},
            "metadata": {},
        },
        "events": [
            {
                "id": f"{run_id}-e1",
                "run_id": run_id,
                "seq": 1,
                "type": "run_started",
                "started_at": "2026-09-13T00:00:00Z",
                "input": {"task": "investigate"},
            },
            {
                "id": f"{run_id}-e2",
                "run_id": run_id,
                "seq": 2,
                "type": "model_call",
                "name": "test-model",
                "started_at": "2026-09-13T00:00:01Z",
                "output": {
                    "text": "calling tool",
                    "tool_calls": [{"id": "c1", "name": "query", "args": {"q": "x"}}],
                },
                "tokens": {"input": 10, "output": 5, "total": 15},
                "effect_source": "live",
            },
            {
                "id": f"{run_id}-e3",
                "run_id": run_id,
                "seq": 3,
                "type": "tool_call",
                "name": "query",
                "started_at": "2026-09-13T00:00:02Z",
                "input": {"args": {"q": "x"}},
                "output": {"text": "result"},
                "side_effect": "read",
                "effect_source": "live",
            },
            {
                "id": f"{run_id}-e4",
                "run_id": run_id,
                "seq": 4,
                "type": "run_finished",
                "started_at": "2026-09-13T00:00:05Z",
                "output": {"result": "root cause is X", "status": "succeeded"},
            },
        ],
    }
    payload["run"].update(run_overrides)
    return payload


os.environ.setdefault("AFR_SEED_ON_STARTUP", "false")
