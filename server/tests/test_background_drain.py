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

## 这一轮收尾：等待集上剩下的三处缺口

第 9 轮的审核（PR #19 之后）用同一把尺子（改掉实现 → 看测试是否变红）量出三处同类的残余缺口：池计数只钉了
`cases` / `suites` 两个池、`wait_idle` 开头那次 `install()`（换上的执行器也要数得到）
没有守护、`background.start` 的「在锁里 start」语义可以静默移除。三条各自对应一个**实测
存活过的变异**：

* `test_a_replay_pool_task_is_in_the_waiting_set`：从池计数循环里删掉 `replay_runner` → 必红；
* `test_a_mid_test_executor_swap_is_tracked_by_the_drain`：删掉 `wait_idle` 开头那次
  `install()` → 必红（换上的执行器从此收不到包裹，交给它的任务对 drain 是隐形的）；
* `test_registration_and_start_have_no_gap`：把 `background.start` 的 `start()` 挪到锁外
  → 必红（登记与开跑之间出现缝：drain 在那段窗口里读到的是空集，会说「都落地了」）。

## 等待集的两半都要有守护

第 9 轮审核指出：上面这些只覆盖了登记表那一半。于是这里按「等待集的每一半 + 两条收紧 +
一条自己声称的语义」分别补齐——每一条都对应一个**实测存活过的变异**：

* `test_a_case_pool_task_is_in_the_waiting_set` / `test_a_suite_pool_task_is_in_the_waiting_set`：
  在 `install()` 开头 `return`（彻底停用三个池的命名计数）→ 池任务不可见 → 必红；
* `test_an_unlanded_task_fails_the_swap`：把 `assert_idle` 的 `raise` 降级回 `warnings.warn`
  → 必红（「等不到就判失败」这条收紧不能只写在文档里）；
* `test_join_all_waits_for_threads_started_while_waiting`：把 `join_all` 的循环重查退化成
  「只等第一轮快照」→ 必红（等待期间新起的线程不能漏）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
#: 播种录制的分叉点。与 `test_replay_budget_api.py` 用的是同一条种子录制：从这一步起
#: 复现模式下不需要真的调用模型（离线），因此这条回放测试不花任何模型费用。
SEED_REPLAY_FROM_SEQ = 15


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


class Stall:
    """把一个后台任务卡住：`entered` 由任务置位，`release` 由测试放行。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()


@pytest.fixture()
def pool_stall() -> Iterator[Stall]:
    """池任务卡点：测试结束（无论成败）一定放行。

    失败路径上更要紧：卡住的池任务会让拆除里的 drain 白等一轮超时，失败信息反而更难读。
    """

    gate = Stall()
    try:
        yield gate
    finally:
        gate.release.set()


def _stall(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, gate: Stall) -> None:
    """把池里那个真任务卡住：放行之后照常执行，卡的只是「任务在飞」这个窗口。"""

    real = getattr(module, name)

    def stalling(*args: Any, **kwargs: Any) -> Any:
        gate.entered.set()
        gate.release.wait(timeout=GATE_TIMEOUT_SECONDS)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, stalling)


def _assert_drain_waits(background_work: Any, release: threading.Event, what: str) -> None:
    """① 卡点没放行时 drain 不能返回；② 放行之后 drain 返回空（都落地了）。"""

    leftover: list[str] = []
    finished = threading.Event()

    def drain() -> None:
        leftover.extend(background_work.wait_idle(timeout=GATE_TIMEOUT_SECONDS))
        finished.set()

    drainer = threading.Thread(target=drain, name="test-drainer", daemon=True)
    drainer.start()
    try:
        assert not finished.wait(timeout=STILL_BLOCKED_WINDOW_SECONDS), (
            f"{what}还活着，drain 却已经返回了：换库会与它并发。"
            f"此刻等待集={background_work.outstanding()}"
        )
    finally:
        release.set()

    assert finished.wait(timeout=GATE_TIMEOUT_SECONDS), f"放行之后 drain 仍然没有返回（{what}）"
    assert leftover == [], f"drain 说还有没落地的后台工作（{what}）：{leftover}"


def _create_case(client: Any, make_run: Any) -> str:
    """建一条最小可执行的用例（与 test_suites.py 的样板同一套）。"""

    client.post("/v1/ingest", json=make_run())
    return client.post(
        "/v1/cases",
        json={"name": "用例", "source_run_id": "run-1", "assertions": [{"type": "no_error"}]},
    ).json()["id"]


def _wait_for_seeded_parent(client: Any, timeout: float = 90.0) -> dict[str, Any]:
    """播种跑在后台：等那条已完成、自己没有父 Run 的种子录制出现（回放要拿它当父）。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in client.get("/v1/runs").json()["runs"]:
            run = item["run"]
            if run["parent_run_id"] is None and run["status"] == "succeeded":
                return run
        time.sleep(0.2)
    raise AssertionError(f"播种未在 {timeout:.0f}s 内产出可回放的父 Run")


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

    # 播种还活着（卡点没放行、也没落地）时 drain 就必须一直没返回：修复被移除时它立刻
    # 返回，这里必红——那正是「换库会与后台任务并发」的那一刻。
    _assert_drain_waits(background_work, seed_gate.release, "播种线程")

    # 落地证据：drain 返回时，播种的产物已经在这个测试的库里——而不是在别人的库上。
    cases = gated_seed_client.get("/v1/cases").json()["cases"]
    assert cases, "drain 返回时播种还没有落地"
    assert gated_seed_client.get("/v1/runs").json()["total"] >= 1


# ---------------------------------------------------------------- 等待集的另一半：线程池


def test_a_case_pool_task_is_in_the_waiting_set(
    client: Any,
    make_run: Any,
    monkeypatch: pytest.MonkeyPatch,
    background_work: Any,
    pool_stall: Stall,
) -> None:
    """在飞的**池任务**也必须在等待集里，而且是被点了名的。

    等待集有两半：登记表与三个线程池的命名计数。这里钉池那一半——在 `install()` 开头
    加一句 `return`（彻底停用池计数）时，这条必红：池任务会重新变成「没人看见的后台
    工作」，也就是 issue #18 的原始形态。
    """

    from afr_server import cases as cases_module

    case_id = _create_case(client, make_run)
    _stall(monkeypatch, cases_module, "_execute", pool_stall)
    cases_module.submit_case_run(case_id)

    assert pool_stall.entered.wait(timeout=GATE_TIMEOUT_SECONDS), "用例池任务没有开始"
    assert any(item.startswith("线程池:cases") for item in background_work.outstanding()), (
        f"在飞的池任务不在等待集里：{background_work.outstanding()}"
    )
    assert background_work.pending.get("线程池:cases") == 1, background_work.pending

    _assert_drain_waits(background_work, pool_stall.release, "用例池任务")

    # 落地证据：drain 返回时结论已经写在**这个测试的**库里。
    status = client.get(f"/v1/cases/{case_id}").json()["last_status"]
    assert status not in (None, "running"), f"drain 返回时用例还没有落地（status={status}）"


def test_a_suite_pool_task_is_in_the_waiting_set(
    client: Any,
    make_run: Any,
    monkeypatch: pytest.MonkeyPatch,
    background_work: Any,
    pool_stall: Stall,
) -> None:
    """套件池那一半同理：整批的执行也走池，第 7 轮正是在这里积压成事实。"""

    from afr_server import suites as suites_module

    case_id = _create_case(client, make_run)
    _stall(monkeypatch, suites_module, "_run_item", pool_stall)

    body = client.post(
        "/v1/suites", json={"case_ids": [case_id], "conditions": [{"model": "m"}]}
    ).json()

    assert pool_stall.entered.wait(timeout=GATE_TIMEOUT_SECONDS), "套件池任务没有开始"
    assert any(item.startswith("线程池:suites") for item in background_work.outstanding()), (
        f"在飞的池任务不在等待集里：{background_work.outstanding()}"
    )

    _assert_drain_waits(background_work, pool_stall.release, "套件池任务")

    detail = client.get(f"/v1/suites/{body['suite_id']}").json()
    assert detail["completed"] == detail["total"] == 1, f"drain 返回时格子还没有落地：{detail}"


def test_a_replay_pool_task_is_in_the_waiting_set(
    seeded_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    background_work: Any,
    pool_stall: Stall,
) -> None:
    """在飞的**回放池任务**也必须在等待集里：池的命名计数是三个池，不是「两个池加上顺便」。

    第 9 轮只钉住了用例池与套件池：从 `install()` 的池计数循环里删掉 `replay_runner` 时
    全量 230 条照样绿。这里走真提交路径（`POST /v1/runs/{id}/replay`）把回放池任务卡在飞的
    状态，断言它在等待集里（点名，不是计数）、drain 不提前返回，而且返回时回放已经落在
    **这个测试的**库里。
    """

    from afr_server import replay_runner

    parent = _wait_for_seeded_parent(seeded_client)
    # 卡住的是池里那个真任务本身：放行后照常执行，卡的只是「任务在飞」这个窗口。
    _stall(monkeypatch, replay_runner, "_run_job", pool_stall)

    response = seeded_client.post(
        f"/v1/runs/{parent['id']}/replay",
        json={"from_seq": SEED_REPLAY_FROM_SEQ, "preset": "reproduce"},
    )
    assert response.status_code == 200, response.text
    replay_run_id = response.json()["run_id"]

    assert pool_stall.entered.wait(timeout=GATE_TIMEOUT_SECONDS), "回放池任务没有开始"
    assert any(item.startswith("线程池:replay_runner") for item in background_work.outstanding()), (
        f"在飞的回放池任务不在等待集里：{background_work.outstanding()}"
    )
    assert background_work.pending.get("线程池:replay_runner") == 1, background_work.pending

    _assert_drain_waits(background_work, pool_stall.release, "回放池任务")

    # 落地证据：drain 返回时回放已经写在这个测试的库里，并且进了终态。
    detail = seeded_client.get(f"/v1/runs/{replay_run_id}").json()
    assert detail["run"]["status"] in {"succeeded", "failed", "aborted"}, detail["run"]


def test_a_mid_test_executor_swap_is_tracked_by_the_drain(
    client: Any,
    make_run: Any,
    monkeypatch: pytest.MonkeyPatch,
    background_work: Any,
    pool_stall: Stall,
) -> None:
    """drain 开始前会重新认一次当前执行器：测试中途换上的那份也要数得到。

    池那一半是「包裹 submit」得来的，所以包裹必须发生在提交**之前**才算数。测试可以中途把
    模块级 `_executor` 换成自己的实现（`test_suites.py` 的 `_ImmediateThreads` 就是），
    换上的那份得在下一轮 drain 开始时被认进来——这就是 `wait_idle` 开头那次 `install()`
    的语义。少了它，从那一刻起提交给它的任务对 drain 是隐形的：`wait_idle` 会立刻说
    「都落地了」，而换库会与那份还在飞的工作并发。

    这条守护覆盖的范围要说清：它钉的是「drain 之后的提交被数到」。在包裹之前就已经提交出去、
    还在飞的任务不在此列——那一段由「先包裹、再提交」的纪律兜着，原因见
    `docs/architecture.md` 第 6 节。
    """

    from afr_server import cases as cases_module

    # 换上一份**还没被计数包裹过**的执行器：它不是模块原来那个对象。
    swapped = ThreadPoolExecutor(max_workers=1, thread_name_prefix="afr-case")
    monkeypatch.setattr(cases_module, "_executor", swapped)
    try:
        # 先 drain 一次：这一步的语义就是「把换上的执行器认进等待集」。
        assert background_work.wait_idle(timeout=GATE_TIMEOUT_SECONDS) == [], (
            "上一轮还有没落地的工作"
        )
        assert getattr(swapped, "_afr_tracked", False) is True, (
            "drain 没有把换上的执行器认进来：从这一刻起的提交会变成隐形工作"
        )

        case_id = _create_case(client, make_run)
        _stall(monkeypatch, cases_module, "_execute", pool_stall)
        cases_module.submit_case_run(case_id)

        assert pool_stall.entered.wait(timeout=GATE_TIMEOUT_SECONDS), "用例池任务没有开始"
        assert any(item.startswith("线程池:cases") for item in background_work.outstanding()), (
            f"换上的执行器里的任务不在等待集里：{background_work.outstanding()}"
        )

        _assert_drain_waits(background_work, pool_stall.release, "换上的执行器里的用例任务")

        # 落地证据：drain 返回时结论已经写在**这个测试的**库里。
        status = client.get(f"/v1/cases/{case_id}").json()["last_status"]
        assert status not in (None, "running"), f"drain 返回时用例还没有落地（status={status}）"
    finally:
        # 放掉池的等待（不 join）：就算卡住也不会把拆除拖成一轮白等的超时。
        swapped.shutdown(wait=False)


def test_an_unlanded_task_fails_the_swap(
    background_work: Any, background_leak: type[BaseException], app_env: Path
) -> None:
    """「等不到就判失败」这条收紧本身也要被钉住。

    把 `assert_idle` 里的 `raise BackgroundLeak` 降级回 `warnings.warn`，这条立刻红：门禁
    又会变回「报一条警告、照样换库」，而上一轮就是这么漏过去的。只是警告的话，跑绿不等于
    不变量成立。
    """

    release = threading.Event()
    background.start("afr-test-never-lands", lambda: release.wait(timeout=GATE_TIMEOUT_SECONDS))
    try:
        with pytest.raises(background_leak) as excinfo:
            background_work.assert_idle(timeout=0.3)
        message = str(excinfo.value)
        assert "afr-test-never-lands" in message, f"诊断里没有任务名：{message}"
        assert app_env.name in message, f"诊断里没有库名：{message}"
    finally:
        release.set()

    assert background_work.wait_idle(timeout=GATE_TIMEOUT_SECONDS) == [], "放行之后仍有没落地的工作"


def test_join_all_waits_for_threads_started_while_waiting(app_env: Path) -> None:
    """等一个线程的过程中新起的线程也不能漏（`join_all` 的循环重查）。

    把 `join_all` 退化成「只看一眼快照、join 完就返回」时，这条必红：第一个线程在结束前
    起了第二个，而等待返回时第二个还在跑。这条不是我凭空加的语义——`background.join_all`
    的 docstring 自己写着「等待期间可能又起了新的线程（播种线程自己也可能触发别的后台
    路径），只等第一轮会漏掉它们」，那就得有守护。
    """

    hold_first = threading.Event()
    second_started = threading.Event()
    second_finished = threading.Event()
    release_second = threading.Event()

    def second_body() -> None:
        second_started.set()
        release_second.wait(timeout=GATE_TIMEOUT_SECONDS)
        second_finished.set()

    def first_body() -> None:
        hold_first.wait(timeout=GATE_TIMEOUT_SECONDS)
        background.start("afr-test-second", second_body)

    background.start("afr-test-first", first_body)
    joined: list[list[str]] = []
    join_entered = threading.Event()
    finished = threading.Event()

    def join() -> None:
        join_entered.set()
        joined.append(background.join_all(timeout=GATE_TIMEOUT_SECONDS))
        finished.set()

    joiner = threading.Thread(target=join, name="test-joiner", daemon=True)
    joiner.start()
    try:
        assert join_entered.wait(timeout=GATE_TIMEOUT_SECONDS), "join 线程没有起来"
        # 让 join_all 先取到快照并等在第一个线程上。万一这一步没赶上，输的方向是「这条测试
        # 抓不到退化实现」（假绿），不是假红。
        time.sleep(0.1)
        hold_first.set()
        assert second_started.wait(timeout=GATE_TIMEOUT_SECONDS), "第一个线程没有起第二个"
        assert not finished.wait(timeout=STILL_BLOCKED_WINDOW_SECONDS), (
            "join_all 只看了一眼快照就返回了：等待期间新起的线程会被漏掉"
        )
    finally:
        hold_first.set()
        release_second.set()

    assert finished.wait(timeout=GATE_TIMEOUT_SECONDS), "放行之后 join_all 仍未返回"
    # 更硬的一条：join_all 返回的那一刻，新起的线程必须已经落地（而不是「返回时还在跑」）。
    assert second_finished.is_set(), "join_all 返回时新起的线程还没有落地"
    assert joined == [[]], f"join_all 说还有没落地的线程：{joined}"


def test_registration_and_start_have_no_gap(app_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """「登记」与「开跑」之间没有缝：锁还握着的时候，drain 说不出「都落地了」。

    `background.start` 在锁里 start，而这个顺序是有语义的：`Thread.start()` 返回时线程
    已经活着（`is_alive()` 为真），因此 drain 一旦拿到锁，看到的就是一个可 join 的对象。
    把 `start()` 挪到锁外（或者干脆先开跑再登记），中间就多出一段窗口：登记表里有它、
    `is_alive()` 还是假——而 `live()` 正是按 `is_alive()` 过滤的，于是 drain 会在那段
    窗口里读到空集、宣布「都落地了」，那个线程随后才开跑，换库恰好与它并发。

    这里把 `Thread.start()` 卡住，把那段窗口撑到可观测：`start()` 没返回之前，drain 必须
    **拿不到**等待集（给出结论这件事本身要等锁），放行之后等待集里必须正好是那个线程，
    而不是「登记了但还没开跑」被过滤成空集。
    """

    real_start = threading.Thread.start
    start_entered = threading.Event()
    release_start = threading.Event()
    body_entered = threading.Event()
    release_body = threading.Event()

    def delayed_start(thread: threading.Thread) -> None:
        # 只卡这一条线程：别的线程（包括见证者自己起的）照常起。
        if thread.name != "afr-test-gap":
            return real_start(thread)
        start_entered.set()
        assert release_start.wait(timeout=GATE_TIMEOUT_SECONDS), "卡住的 Thread.start 没被放行"
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", delayed_start)

    def body() -> None:
        # 进到线程体里之后占住不放：等待集里那个名字在整个观察窗口内都活着。
        body_entered.set()
        release_body.wait(timeout=GATE_TIMEOUT_SECONDS)

    def start_it() -> None:
        background.start("afr-test-gap", body)

    observed: list[list[str]] = []
    probe_done = threading.Event()

    def probe() -> None:
        # 这就是 drain 读等待集的方式：一次 names()（拿不到锁就会被挡在门外）。
        observed.append(background.names())
        probe_done.set()

    starter = threading.Thread(target=start_it, name="test-starter", daemon=True)
    prober = threading.Thread(target=probe, name="test-prober", daemon=True)
    starter.start()
    try:
        assert start_entered.wait(timeout=GATE_TIMEOUT_SECONDS), "没有走到 Thread.start()"
        prober.start()
        assert not probe_done.wait(timeout=STILL_BLOCKED_WINDOW_SECONDS), (
            "Thread.start() 还没返回，drain 就已经给出了等待集：「登记」与「开跑」之间有缝，"
            "这一段里还没开跑的线程会被漏掉"
        )

        release_start.set()
        assert probe_done.wait(timeout=GATE_TIMEOUT_SECONDS), "放行之后 drain 仍然拿不到等待集"
        assert observed == [["afr-test-gap"]], (
            f"start 返回之后等待集里必须正好是那个线程，实际={observed}（空集=缝还在）"
        )
        assert body_entered.wait(timeout=GATE_TIMEOUT_SECONDS), "线程没有真的开跑"
    finally:
        release_start.set()
        release_body.set()
        background.join_all(timeout=GATE_TIMEOUT_SECONDS)


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
