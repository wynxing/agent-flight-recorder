"""测试隔离不变量：换库之前，后台工作必须全部落地。

issue #18 的成因不是产品缺陷，而是门禁自己的漏：播种线程（`afr-seed`）是裸
`threading.Thread`，drain 只数三个线程池，永远看不见它。于是它会在下一个测试的库上
接着跑——在 CI 上表现为 90s 超时，同时后台日志里出现 `no such table: cases`。

这里刻意**不**用「连跑 N 次看看能不能撞上」那种证据（本轮缺陷就是这么漏过去的），
而是主动制造「后台工作活过测试」的局面：把播种卡在一个只有测试才能放行的事件上，
然后看 drain 会不会等它。修复被移除时，下面的断言必红——两次运行的输出都留在 PR 里：

* `test_seed_thread_is_in_the_waiting_set` 与 `test_drain_waits_for_the_seed_thread`：
  播种不再被登记 → 它不在等待集里 → drain 立刻返回 → 「drain 在后台工作还活着时就返回了」必红；
* `test_seeded_client_teardown_waits_for_the_seed_thread`：`seeded_client` 的拆除里
  少了 drain → 拆除立刻返回 → 还卡着的播种线程活到见证者的拆除 → 必红；
* `test_leftover_work_is_reported_by_name_and_database`：钉住诊断信息——报错要说清
  「哪个任务、连到了哪个库」，而不是一个超时数字。

另有两条钉住「产品行为不变」：播种仍然异步、仍然不阻塞服务可用（卡住播种时健康检查照样
回话），而 `AFR_SEED_ON_STARTUP=false` 时连线程都不起。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from afr_server import background

#: 卡点的等待上限：正常路径都在毫秒级放行，这个数字只负责「测试失败时不留挂死的线程」。
GATE_TIMEOUT_SECONDS = 30.0
#: 判定「drain 确实卡住了」的观察窗口：卡点不放开，drain 就必须一直没返回。
STILL_BLOCKED_WINDOW_SECONDS = 1.0
#: 自动放行卡点的延迟。测试主体几毫秒就跑完，而播种要到这一刻才真的写库：
#: 「测试结束、播种还没落地」这个局面因此是造出来的，不是撞上的。
SEED_GATE_DELAY_SECONDS = 0.5
#: 放行之后，播种还要**占住**这么久才真的写库。
#: 这一条把「换库时它还没落地」变成必然可观测的窗口，而不是赌「它没这么快跑完」。
SEED_LANDING_SECONDS = 0.5


class SeedGate:
    """播种卡点：`started` 由播种线程置位，`release` 由测试（或计时器）放行。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        #: 放行之后还要占住多久才落地。默认 0：只有需要观测「还没落地」的测试才打开它。
        self.landing_seconds = 0.0


@pytest.fixture()
def seed_gate(monkeypatch: pytest.MonkeyPatch) -> SeedGate:
    """把播种卡住，直到有人放行。

    补的是真函数：卡点只负责「先别写库」，播种本身还是那条真实链路（录制 + 建用例 + 跑一次）。
    """

    from afr_server import main

    gate = SeedGate()
    real_seed = main.seed_if_empty

    def gated_seed() -> str | None:
        gate.started.set()
        gate.release.wait(timeout=GATE_TIMEOUT_SECONDS)
        if gate.landing_seconds:
            time.sleep(gate.landing_seconds)
        return real_seed()

    monkeypatch.setattr(main, "seed_if_empty", gated_seed)
    return gate


@pytest.fixture()
def seed_gate_timer(seed_gate: SeedGate) -> Iterator[SeedGate]:
    """给卡点装一个计时器，到点自动放行（用于「测试已经结束、播种还在跑」的局面）。"""

    seed_gate.landing_seconds = SEED_LANDING_SECONDS
    timer = threading.Timer(SEED_GATE_DELAY_SECONDS, seed_gate.release.set)
    timer.daemon = True
    timer.start()
    try:
        yield seed_gate
    finally:
        timer.cancel()
        # 兜底：失败路径也不留一个还卡着的播种线程（否则拆除会白等一轮超时）。
        seed_gate.release.set()


@pytest.fixture()
def gated_seed_client(seed_gate: SeedGate, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """一个播种被卡住的客户端：用来主动制造「后台任务活过测试」的局面。

    依赖 `seed_gate` 保证补丁先于应用启动生效。这里刻意**不**做 drain——等待正是下面
    这些测试要检验的东西；兜底放在 finally 里。
    """

    from fastapi.testclient import TestClient

    from afr_server import config, db, main

    monkeypatch.setenv("AFR_DB_PATH", str(tmp_path / "afr-gated-seed.db"))
    monkeypatch.setenv("AFR_SEED_ON_STARTUP", "true")
    config.get_settings.cache_clear()
    db.reset_engine()
    db.init_db()

    try:
        with TestClient(main.app) as client:
            assert seed_gate.started.wait(timeout=GATE_TIMEOUT_SECONDS), "播种线程没有起来"
            yield client
    finally:
        # 兜底：就算上面的断言失败，也不把卡住的播种留给下一个测试。
        seed_gate.release.set()
        background.join_all(timeout=GATE_TIMEOUT_SECONDS)
        db.reset_engine()
        config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def seed_thread_witness() -> Iterator[None]:
    """见证者：轮到它拆除时，后台工作必须都已经落地。

    autouse 让它比测试自己请求的 fixture 更早建立、更晚拆除，因此它的拆除时机正好落在
    「换库之后、下一个测试之前」——也就是这条不变量的观察点。少了 `seeded_client` 拆除里
    的那次 drain 时，播种线程还会在这里被看见。
    """

    yield
    alive = background.names()
    assert not alive, f"换库之后还有后台线程活着：{alive}（换库会与它并发）"


def test_seed_thread_is_in_the_waiting_set(
    seed_gate: SeedGate, gated_seed_client: Any, background_work: Any
) -> None:
    """机制：播种线程在等待集里，而且是**被点了名的**（不是一个计数）。

    这条比「drain 会等」更靠下一层：等它之所以可能，是因为它在登记表里、有一个线程对象
    可以 join。等待集里出现的是名字，因此报错时能说出是谁，而不是「还有 1 个」。
    """

    assert seed_gate.started.is_set(), "卡点没生效：播种线程根本没有起来"
    assert "后台线程:afr-seed" in background_work.outstanding(), (
        f"播种线程不在等待集里：{background_work.outstanding()}"
    )
    assert background.names() == ["afr-seed"], "登记表里没有播种线程：没有一个可等的对象"

    # 顺带把「播种仍然不阻塞服务可用」钉在这里：播种被卡得死死的，服务照样回健康检查。
    health = gated_seed_client.get("/v1/health").json()
    assert health["ok"] is True
    assert health["seed_on_startup"] is True
    assert gated_seed_client.get("/v1/runs").json()["total"] == 0, "播种已经写库了：卡点没生效"


def test_seeding_off_creates_no_seed_thread(client: Any, background_work: Any) -> None:
    """`AFR_SEED_ON_STARTUP=false` 时行为不变：连线程都不起（app_env 的默认）。"""

    assert client.get("/v1/health").json()["seed_on_startup"] is False
    assert background.names() == [], f"关掉播种后不该有后台线程：{background.names()}"
    assert background_work.pending == {}, f"关掉播种后不该有后台任务：{background_work.pending}"


def test_drain_waits_for_the_seed_thread(
    seed_gate: SeedGate, gated_seed_client: Any, background_work: Any
) -> None:
    """行为：播种还活着时 drain 不能返回；它落地之后才放行，产物就在这个测试的库里。"""

    drained: list[str] = []
    finished = threading.Event()

    def drain() -> None:
        drained.extend(background_work.wait_idle(timeout=GATE_TIMEOUT_SECONDS))
        finished.set()

    drainer = threading.Thread(target=drain, name="test-drainer", daemon=True)
    drainer.start()
    try:
        # 关键反证：后台工作还活着（卡点没放行、也没落地），drain 就必须一直没返回。
        # 修复被移除时它立刻返回，这里必红——这正是「换库会与后台任务并发」那一刻。
        assert not finished.wait(timeout=STILL_BLOCKED_WINDOW_SECONDS), (
            "drain 在后台工作还活着时就返回了：换库会与它并发。"
            f"此刻等待集={background_work.outstanding()}，登记表={background.names()}，"
            f"播种是否已启动={seed_gate.started.is_set()}"
        )
    finally:
        seed_gate.release.set()

    assert finished.wait(timeout=GATE_TIMEOUT_SECONDS), "放行之后 drain 仍然没有返回"
    assert drained == [], f"drain 说还有没落地的后台工作：{drained}"

    # 落地证据：drain 返回时，播种的产物已经在这个测试的库里——而不是在别人的库上。
    cases = gated_seed_client.get("/v1/cases").json()["cases"]
    assert cases, "drain 返回时播种还没有落地"
    assert gated_seed_client.get("/v1/runs").json()["total"] >= 1


def test_seeded_client_teardown_waits_for_the_seed_thread(
    seed_gate_timer: SeedGate, seeded_client: Any
) -> None:
    """测试主体结束时播种还在跑：`seeded_client` 的拆除必须等它，而不是直接换库。

    这里靠 `seed_gate_timer`（依赖关系保证补丁先于应用启动生效）把播种卡到主体之后：
    主体结束时它还没写库，而见证者（`seed_thread_witness`）随后在「换库之后」这个位置
    检查还有没有活着的后台线程。拆除里少了那次 drain 时，播种线程一定还在——放行之后它
    还要占住 `SEED_LANDING_SECONDS` 才落地，而见证者紧跟拆除跑，这个窗口不是赌出来的。
    """

    assert seed_gate_timer.started.is_set(), "卡点没生效：播种线程根本没有起来"
    assert background.names() == ["afr-seed"], (
        f"播种线程不在等待集里（没被登记、或者卡点没生效）：{background.names()}"
    )
    # 「服务在播种完成前就已经可用」：这一刻播种还没写库，健康检查已经回话了。
    assert seeded_client.get("/v1/health").json()["ok"] is True
    assert seeded_client.get("/v1/runs").json()["total"] == 0, "播种已经写库了：卡点没生效"


def test_leftover_work_is_reported_by_name_and_database(background_work: Any, app_env: Any) -> None:
    """失败可诊断：报错要说清「哪个任务、连到了哪个库」，而不是一个超时数字。"""

    from afr_server import db

    assert db.bound_db_path() is not None, "诊断读的是 engine 真实绑定的库"

    release = threading.Event()
    background.start("afr-test-stuck", lambda: release.wait(timeout=GATE_TIMEOUT_SECONDS))
    try:
        leftover = background_work.wait_idle(timeout=0.2)
        assert leftover, "卡住的后台任务没有被 drain 看见"
        assert any("afr-test-stuck" in item for item in leftover), leftover

        message = background_work.describe(leftover)
        assert "afr-test-stuck" in message, f"诊断里没有任务名：{message}"
        assert app_env.name in message, f"诊断里没有库名：{message}"
        assert "engine" in message, f"诊断没有说清 engine 当时绑在哪个库：{message}"
    finally:
        release.set()
        background.join_all(timeout=GATE_TIMEOUT_SECONDS)
