"""每个测试用一份全新的 SQLite 库，避免用例之间互相污染。

## 一条不变量：换库之前，后台工作必须全部落地

服务有好几条「提交即返回」的路径——回放、用例、批量套件、启动播种——真正的工作在后台
跑，请求不等它们。测试可以在断言完「提交立刻返回」之后马上结束（那正是它要验证的行为），
但那些后台工作会活过这个测试。而 engine 是按**当前**设置懒建的（`db.get_engine`），
于是它们会在下一个测试里重建 engine、连到下一个测试的库上去写：症状是后台日志里的
`no such table: cases`，或者某个测试干等到超时。issue #18 就是这条在 CI 上发作的。

这里因此守一条不变量：**换库（`db.reset_engine`）之前，drain 必须先看到后台工作全部
落地**。等待集由两部分组成：

* 线程池：`cases` / `suites` / `replay_runner` 的 `submit` 被套了一层命名计数；
* 登记的后台线程：应用自己起的线程（目前是播种 `afr-seed`）走
  `afr_server.background.start`，drain 直接 join 它们。

只认识线程池的 drain 是不够的：播种就是裸 `threading.Thread`，谁都没数到它。**加了新的
后台路径就必须让它可被看见**（走 `background.start`，或在这里登记），否则这一层会安静地
漏掉它。已知未纳入的路径登记在 `docs/architecture.md` 第 6 节；`stray_threads()` 是给
这类遗漏留的诊断网，只在报错时说话。

等待有上限（`DRAIN_TIMEOUT_SECONDS`）：超时不是「再等久一点」的问题，而是不变量已经被
破坏，因此**判失败**并说清是谁、连到了哪个库，而不是留一句容易被忽略的警告。
"""

from __future__ import annotations

import os
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# 登记表是模块级单例，测试与 conftest 必须拿到同一个对象，因此这里直接 import 而不是
# 在方法里懒导入（`afr_server.background` 只定义登记表，不读设置、不连库）。
from afr_server import background
from afr_server.db import bound_db_path

#: 线程池 worker 的名字前缀：它们在池子的整个生命周期里都活着（空闲时也在），
#: 因此「线程还活着」不能当成「还有活儿没干完」。
POOL_THREAD_PREFIXES = ("afr-case", "afr-suite", "afr-replay")
DRAIN_TIMEOUT_SECONDS = 60.0


class BackgroundLeak(AssertionError):
    """后台工作在换库前没落地。不变量被破坏时，这一轮的结论不再可信。"""


class BackgroundWork:
    """数一数还有多少后台工作在跑，并在换库之前等它们落地。

    有些测试提交一批格子之后立刻断言「提交不阻塞」就结束了——那正是它要验证的行为，
    因此不能要求它自己等批次跑完。但那些后台线程会活过这个测试：它们下一次调用
    `session_scope` 时读到的是**下一个测试**的设置（engine 是按当前设置懒建的），
    于是会连到下一个测试的库上去写。再叠上 `init_db` 的 `PRAGMA journal_mode=WAL`
    需要一次排他锁，就会偶发 `database is locked`——表现为下一个测试在 fixture 里
    直接 ERROR（不是任何断言失败），也就是门禁在没有改动任何行为的情况下变红。

    「谁在跑」记成**名字**而不是一个数字：失败时要能说出是哪个任务，而不是给一个
    「还有 1 个」让人去猜。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, int] = {}
        self._last_bound_db: str | None = None
        self._leak_reported = False

    def reset(self) -> None:
        """每个测试开始时清一次：上一个测试的判定不该影响这一个。"""

        self._leak_reported = False

    def install(self) -> None:
        """给三个线程池的 submit 套一层命名计数。可重复调用（幂等）。

        drain 之前会再调一次：测试可能中途把 `_executor` 换掉（`test_suites.py` 的
        `_ImmediateThreads` 就是），换上的那个也要数得到。
        """

        from afr_server import cases, replay_runner, suites

        for module in (cases, suites, replay_runner):
            executor = getattr(module, "_executor", None)
            if executor is None or getattr(executor, "_afr_tracked", False):
                continue
            label = f"线程池:{module.__name__.rsplit('.', 1)[-1]}"
            real_submit = executor.submit

            def submit(fn, /, *args, _real=real_submit, _label=label, **kwargs):
                self._enter(_label)

                def run():
                    try:
                        return fn(*args, **kwargs)
                    finally:
                        self._exit(_label)

                try:
                    return _real(run)
                except BaseException:
                    self._exit(_label)
                    raise

            executor.submit = submit
            executor._afr_tracked = True

    def _enter(self, label: str) -> None:
        with self._lock:
            self._pending[label] = self._pending.get(label, 0) + 1

    def _exit(self, label: str) -> None:
        with self._lock:
            left = self._pending.get(label, 0) - 1
            if left > 0:
                self._pending[label] = left
            else:
                self._pending.pop(label, None)

    @property
    def pending(self) -> dict[str, int]:
        """此刻线程池里没跑完的任务（名字 -> 个数）。"""

        with self._lock:
            return dict(self._pending)

    def outstanding(self) -> list[str]:
        """此刻还没落地的后台工作：池里的任务 + 还活着的登记线程。"""

        labels = [f"{label}×{count}" for label, count in sorted(self.pending.items())]
        labels.extend(f"后台线程:{name}" for name in background.names())
        return labels

    def stray_threads(self) -> list[str]:
        """还活着、但不在等待集里的 `afr-*` 线程。只做诊断，不参与等待。

        等待集是登记表；这里是给「漏登记的后台路径」留的一张诊断网：万一以后再有人起了
        一个绕过登记的后台线程，报错信息里至少能看到它的名字，而不是一个超时数字。
        常驻的线程池 worker 空闲时也活着，按前缀排除。
        """

        ignored = set(background.names())
        return [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("afr-")
            and thread.name not in ignored
            and not any(thread.name.startswith(prefix) for prefix in POOL_THREAD_PREFIXES)
        ]

    def wait_idle(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> list[str]:
        """等后台工作落地；返回超时后仍没落地的（空列表 = 都落地了）。

        登记线程是可等待的，直接 join，不靠轮询；线程池只给了计数，那部分仍然是轮询。
        诊断（哪个库）在**超时的那一刻**采集，因为那时候 engine 还绑着有意义的 URL。
        """

        self.install()
        self._last_bound_db = bound_db_path() or self._last_bound_db
        deadline = time.monotonic() + timeout
        while True:
            if not self.pending and not background.names():
                return []
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.outstanding()
            if background.names():
                background.join_all(timeout=remaining)
                continue
            time.sleep(0.02)

    def describe(self, outstanding: list[str] | None = None) -> str:
        """一句能直接读的失败说明：谁没落地、连到了哪个库。"""

        labels = self.outstanding() if outstanding is None else outstanding
        stray = self.stray_threads()
        note = (
            f"；另有不在等待集里的 afr-* 线程：{', '.join(stray)}（未登记，仅诊断）"
            if stray
            else ""
        )
        return (
            f"后台工作没有在换库前落地：{', '.join(labels) or '（已落地）'}。"
            f"engine 之前绑定={self._last_bound_db or '（还没建过 engine）'}。"
            "换库会与它们并发：那些线程会在下一个测试里重建 engine、写到别人的库上"
            f"{note}。"
        )

    def assert_idle(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> None:
        """换库前的纪律：等不到就判失败，并说清是谁、连到了哪个库。

        同一次漏只报一次：拆 engine 的每一步都该确认前提成立，但让失败路径变成两倍超时、
        或者把同一条诊断印两遍，只会让现场更难读。
        """

        if self._leak_reported:
            return

        leftover = self.wait_idle(timeout)
        if leftover:
            self._leak_reported = True
            raise BackgroundLeak(self.describe(leftover))


_background = BackgroundWork()


def _drain_previous_test() -> None:
    """换库前再等一次上一个测试留下的后台工作。

    这里只告警不判失败：真漏了的话，漏掉的那个测试自己的拆除已经判过一次；在下一个
    测试的 fixture 里再判一次，只会把同一个问题报成两个测试失败，反而不好定位。
    """

    _background.install()
    leftover = _background.wait_idle()
    if leftover:
        warnings.warn(_background.describe(leftover), stacklevel=3)


@pytest.fixture(autouse=True)
def track_background_work() -> Iterator[None]:
    """整个测试期间都数着后台任务（见 `_BackgroundWork` 的说明）。"""

    _background.install()
    _background.reset()
    yield
    # 这个 fixture 最后拆除（autouse 先建立、后拆除），因此轮到它说话时，各 fixture 的
    # 换库纪律都已经执行过一遍。这里还剩东西，就是真的有后台工作漏过了换库点：
    # 报名字、报库，别让它变成一个没人看的警告。
    _background.assert_idle()


@pytest.fixture()
def background_work() -> BackgroundWork:
    """把 drain 暴露给测试：主动制造「后台工作活过测试」的局面时需要它。"""

    return _background


@pytest.fixture()
def background_leak() -> type[BackgroundLeak]:
    """`pytest.raises` 要认的那个失败类型，由 fixture 给出。

    不走 `from conftest import ...`：仓库里有多个 tests 目录、同名模块会互相覆盖
    （`examples/langgraph_sre_agent/tests/conftest.py` 就是这个名字），跑全量时拿到的可能
    是别人的 `conftest`。与 `make_run` 同样的理由，用 fixture 传递。
    """

    return BackgroundLeak

@pytest.fixture()
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # 换库之前先等上一个测试留下的后台任务落地：否则它们会连到这一个测试的库上写。
    _drain_previous_test()

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
        # 等不到就判失败——「这次换库是安全的」这个前提已经不成立了。
        _background.assert_idle()
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

    它**不经过 app_env**，因此换库纪律要在这里单独执行一遍：播种线程经常活到测试断言
    之后——父 Run 一落库，测试就可以走完了，播种还要接着建用例、跑一次回放（这正是
    issue #18 里那 90s 超时的成因）。不在这里等，它会连到下一个测试的库上。
    """

    _drain_previous_test()
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
        # 拆 engine 之前必须等播种（以及任何后台工作）落地，等不到就判失败。
        _background.assert_idle()
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
