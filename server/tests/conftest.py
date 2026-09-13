"""每个测试用一份全新的 SQLite 库，避免用例之间互相污染。"""

from __future__ import annotations

import os
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

#: 服务端把回放、用例与批量都交给这几个模块级线程池（名字前缀同时也是这里识别
#: 「还有活儿在飞」的依据）。
BACKGROUND_MODULES = ("cases", "suites", "replay_runner")
DRAIN_TIMEOUT_SECONDS = 60.0


class _BackgroundWork:
    """数一数还有多少后台任务在跑，并在换库之前等它们落地。

    有些测试提交一批格子之后立刻断言「提交不阻塞」就结束了——那正是它要验证的行为，
    因此不能要求它自己等批次跑完。但那些后台线程会活过这个测试：它们下一次调用
    `session_scope` 时读到的是**下一个测试**的设置（engine 是按当前设置懒建的），
    于是会连到下一个测试的库上去写。再叠上 `init_db` 的 `PRAGMA journal_mode=WAL`
    需要一次排他锁，就会偶发 `database is locked`——表现为下一个测试在 fixture 里
    直接 ERROR（不是任何断言失败），也就是门禁在没有改动任何行为的情况下变红。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = 0

    def install(self) -> None:
        """给三个线程池的 submit 套一层计数。可重复调用（幂等）。"""

        from afr_server import cases, replay_runner, suites

        for module in (cases, suites, replay_runner):
            executor = getattr(module, "_executor", None)
            if executor is None or getattr(executor, "_afr_tracked", False):
                continue
            real_submit = executor.submit

            def submit(fn, /, *args, _real=real_submit, **kwargs):
                with self._lock:
                    self._pending += 1

                def run():
                    try:
                        return fn(*args, **kwargs)
                    finally:
                        with self._lock:
                            self._pending -= 1

                try:
                    return _real(run)
                except BaseException:
                    with self._lock:
                        self._pending -= 1
                    raise

            executor.submit = submit
            executor._afr_tracked = True

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def wait_idle(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pending == 0:
                return
            time.sleep(0.02)
        warnings.warn(
            f"还有 {self.pending} 个后台任务没有落地（等了 {timeout:.0f}s）：换库可能与它们并发，"
            "这一轮的结论不再可信。",
            stacklevel=2,
        )


_background = _BackgroundWork()


@pytest.fixture(autouse=True)
def track_background_work() -> Iterator[None]:
    """整个测试期间都数着后台任务（见 `_BackgroundWork` 的说明）。"""

    _background.install()
    yield

@pytest.fixture()
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # 换库之前先等上一个测试留下的后台任务落地：否则它们会连到这一个测试的库上写。
    _background.install()
    _background.wait_idle()

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
        # 拆 engine 之前同样等它们落地：engine 一换，还在跑的线程会重新指向新库。
        _background.wait_idle()
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
