"""批次级聚合预算：整批的上限、硬停与「未启动」格子的如实处置。

这个文件守四件事（issue #14 的验收）：

* **不声明整批上限时逐字一致**：调度、状态取值、判定、四态计数都不变；
* **到点不再启动新格子**，已启动的格子跑完并如实记账，整批实际调用 <= 上限；
* **未启动的格子如实标注**：它是 not_started，不是 failed，也不是「拿不到结论」，
  并且进汇总、进下钻、有稳定机器值（三处闭集一致）；
* **提交前可预估整批**：缺数据时是「无法预估」+ 原因，不拿部分数据凑一个确定数字。

大部分判定用「会真的花掉 N 次调用」的假执行器驱动：整批预算的判定、账本、格子落库与聚合
全是真代码，被替换的只有「真跑一次回放」这一步。真正产生路径（离线剧本模型）另有一组
端到端测试兜底。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from afr_server import suites
from afr_server.suites import (
    NOT_STARTED,
    NOT_STARTED_LABEL,
    NOT_STARTED_REASON,
    OPEN_STATUSES,
    STATUS_ORDER,
    VERDICTS,
)
from agent_flight_recorder.replay.reasons import InconclusiveReason

ROOT = Path(__file__).resolve().parents[2]

#: 播种出来的示例 Agent 与它的用例起点（分叉点之后还有两次真实模型调用）。
SEED_AGENT = "checkout-api-sre"
SEED_FROM_SEQ = 15
#: 离线剧本模型：不在本地价格表内，因此它的成本必须是「无关」/「未知」而不是 0。
UNPRICED_MODEL = "afr-scripted-sre-v1"
PRICED_MODEL = "gpt-4o-mini"

#: 「未启动」时不该出现的措辞：没跑的格子既不是跑完了，也不是拿不到结论。
BANNED_WORDING = ("跑完了", "全部通过", "拿不到结论")


# ------------------------------------------------------------------ 工具


def _create_cases(client: Any, make_run: Any, count: int = 2) -> list[dict[str, Any]]:
    client.post("/v1/ingest", json=make_run())
    return [
        client.post(
            "/v1/cases",
            json={
                "name": f"用例 {index}",
                "source_run_id": "run-1",
                "assertions": [{"type": "no_error"}],
            },
        ).json()
        for index in range(count)
    ]


def _await_suite(client: Any, suite_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/v1/suites/{suite_id}").json()
        if body["status"] == "finished":
            return body
        time.sleep(0.05)
    raise AssertionError(f"批次没有在 {timeout:.0f}s 内结束：{body}")


def _install_plain_runner(
    monkeypatch: pytest.MonkeyPatch,
    plan: Callable[[str, dict], tuple] | None = None,
) -> list[dict[str, Any]]:
    """**不带整批账本参数**的假执行器：没有声明预算时那条路径不会多传账本。

    用它跑一遍没有声明预算的批次，等于证明那条路径没有多传 `budget` / `shared_ledger`：
    多传了这里会 TypeError，格子会落到 error，测试立刻变红。

    它**会**收到 `definition`：用例集版本化之后，批次路径上每一格都带着提交那一刻冻结的
    定义（见 case_versions.py）。那是版本化的正常输入，不是「没声明预算却多传账本」。
    """

    calls: list[dict[str, Any]] = []

    def runner(
        case_id: str,
        *,
        definition: dict[str, Any] | None = None,
        preset: Any = None,
        model: str | None = None,
        system_prompt: str | None = None,
        on_started: Callable[[str], None] | None = None,
        on_result: Any = None,
        condition: dict[str, Any] | None = None,
    ) -> str:
        run_id = f"run-{len(calls) + 1}"
        calls.append({"case_id": case_id, "preset": preset, "condition": condition})
        if on_started is not None:
            on_started(run_id)
        if on_result is not None:
            verdict, cause = (plan or (lambda cid, cond: ("passed", None)))(case_id, condition or {})
            on_result(verdict, [{"spec": {"type": "no_error"}, "passed": True, "detail": "假执行器"}], cause)
        return run_id

    monkeypatch.setattr(suites, "run_case_blocking", runner)
    return calls


def _install_spending_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    per_cell: int,
    cost_per_call: float | None = None,
) -> list[dict[str, Any]]:
    """一个「每一格真的要花 per_cell 次真实调用」的假执行器。

    用量写进 shared_ledger，而不是自己另记一份——真实路径里这一步正是由 SDK 的账本完成的
    （ReplaySession.record_live_model_call 同时记进格子的账与整批的账）。因此这里替换掉的
    只是「模型真的被调用」这件事，预算判定与记账走的都是真代码。

    额度不够时它只花得起剩下的那部分，并在步边界停下、判 inconclusive（与单次回放的硬停
    同一条规则）：这就是「已启动的格子允许跑完、但不做预测性中断」在额度不够时的样子。
    """

    seen: list[dict[str, Any]] = []

    def runner(
        case_id: str,
        *,
        definition: dict[str, Any] | None = None,
        preset: Any = None,
        model: str | None = None,
        system_prompt: str | None = None,
        budget: Any = None,
        shared_ledger: Any = None,
        on_started: Callable[[str], None] | None = None,
        on_result: Any = None,
        condition: dict[str, Any] | None = None,
    ) -> str:
        run_id = f"run-{len(seen) + 1}"
        # 这一格看得起多少次调用：两个维度都要满足（次数与成本都是发出之前能算清的）。
        calls = per_cell
        if budget is not None:
            if budget.max_model_calls is not None:
                calls = min(calls, budget.max_model_calls)
            if budget.max_cost_usd is not None and cost_per_call:
                # 加一点容差：0.3 / 0.1 这类二进制下不精确的除法不该在测试里决定结论。
                calls = min(calls, int((budget.max_cost_usd + 1e-9) // cost_per_call))
        seen.append({"case_id": case_id, "budget": budget, "calls": calls, "condition": condition})
        if on_started is not None:
            on_started(run_id)
        for _ in range(calls):
            shared_ledger.note_model_call(cost_per_call)
        stopped = calls < per_cell
        if on_result is not None:
            on_result(
                "inconclusive" if stopped else "passed",
                [{"spec": {"type": "no_error"}, "passed": not stopped, "detail": "假执行器"}],
                (
                    InconclusiveReason.from_code(
                        "budget_exceeded",
                        f"已用 {calls} 次真实模型调用（这一格拿到的额度不够跑完）",
                    )
                    if stopped
                    else None
                ),
            )
        return run_id

    monkeypatch.setattr(suites, "run_case_blocking", runner)
    return seen


def _group(body: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    for item in body["groups"]:
        if (item["condition"].get("model") or None) == model:
            return item
    raise AssertionError(f"汇总里没有这个条件：model={model!r}")


def _item(body: dict[str, Any], case_id: str) -> dict[str, Any]:
    for group in body["groups"]:
        for item in group["items"]:
            if item["case_id"] == case_id:
                return item
    raise AssertionError(f"汇总里没有这个格子：{case_id}")


#: 加整批预算之前，批次汇总里就有的键。用来把「新增的东西」钉成一个明确的差集。
_OLD_SUITE_KEYS = {
    "id",
    "status",
    "created_at",
    "finished_at",
    "case_ids",
    "conditions",
    "total",
    "completed",
    "counts",
    "errors",
    "groups",
}
_OLD_GROUP_KEYS = {
    "condition_key",
    "condition",
    "label",
    "total",
    "completed",
    "counts",
    "determinable",
    "undecided",
    "unfinished",
    "determinable_rate",
    "errors",
    "items",
}
_OLD_ITEM_KEYS = {
    "id",
    "case_id",
    "case_name",
    "condition_key",
    "condition",
    "status",
    "run_id",
    "results",
    "cause",
    "started_at",
    "ended_at",
}


# ------------------------------------------------------------------ 不声明预算 = 逐字一致


def test_without_a_batch_budget_the_suite_behaves_exactly_as_before(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不声明整批上限时，套件行为与加这套能力之前逐字一致。

    这条唯一的差别被钉成一个明确的差集：多出三个「什么都没有」的位置
    （汇总的 budget = null、counts 与分组里的 not_started = 0）。格子状态、判定、
    四态计数、生命周期与调用签名全都不变。
    """

    cases = _create_cases(client, make_run, count=2)
    # 假执行器的签名**不含** budget / shared_ledger：那条路径上一旦多传了参数，
    # 这里就会 TypeError -> 格子落 error -> 下面的断言立刻变红。
    calls = _install_plain_runner(monkeypatch)

    suite_id = client.post(
        "/v1/suites",
        json={"case_ids": [case["id"] for case in cases], "conditions": [{"model": "m"}]},
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    assert len(calls) == 2
    assert body["budget"] is None
    assert body["status"] == "finished"
    assert body["completed"] == body["total"] == 2
    assert body["counts"] == {
        "passed": 2,
        "failed": 0,
        "inconclusive": 0,
        "error": 0,
        "pending": 0,
        "running": 0,
        "not_started": 0,
    }
    assert body["errors"] == 0

    group = _group(body, model="m")
    assert group["determinable"] == 2
    assert group["undecided"] == 0
    assert group["unfinished"] == 0
    assert group["not_started"] == 0
    assert group["determinable_rate"] == 1.0

    # 差集就是全部的新增：没有第二个记账对象，也没有第二个计数。
    # case_set 是用例集版本（issue #24）加的：它**与预算无关**，不声明上限的批次同样带着
    # 「这批跑的是哪一版用例」——版本不是预算的附属能力，所以这里它照样在差集里。
    assert set(body) - _OLD_SUITE_KEYS == {"budget", "case_set"}
    assert set(body["counts"]) - {
        "passed",
        "failed",
        "inconclusive",
        "error",
        "pending",
        "running",
    } == {NOT_STARTED}
    assert set(group) - _OLD_GROUP_KEYS == {"not_started"}
    # 格子的新增只有版本归属：它才是被执行的单位，「我跑的是哪一版」不该靠反查套件才知道。
    assert set(group["items"][0]) - _OLD_ITEM_KEYS == {"case_set_version"}
    # 批次层依然只有计数，没有任何跨条件的比率或分数（issue #10 的契约不变）。
    assert {
        key
        for key, value in body.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    } == {"total", "completed", "errors"}


def test_a_batch_without_a_budget_never_marks_a_cell_not_started(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「未启动」不会在没有声明上限的批次里凭空出现（它是预算用尽的产物）。"""

    cases = _create_cases(client, make_run, count=3)
    _install_plain_runner(monkeypatch)

    body = client.post(
        "/v1/suites",
        json={"case_ids": [case["id"] for case in cases]},
    ).json()
    detail = _await_suite(client, body["suite_id"])

    assert detail["completed"] == detail["total"] == 3
    assert all(item["status"] not in OPEN_STATUSES for group in detail["groups"] for item in group["items"])
    assert detail["counts"][NOT_STARTED] == 0


# ------------------------------------------------------------------ 到点不再启动新格子


def test_a_batch_budget_that_runs_out_leaves_the_rest_not_started(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限恰好允许前两格：前两格跑完，后面的格子从未启动。

    每格要花 3 次调用，上限 6 次 -> 恰好两格。第三格启动前账已经到点，因此它不会被启动。
    """

    per_cell = 3
    cases = _create_cases(client, make_run, count=4)
    seen = _install_spending_runner(monkeypatch, per_cell=per_cell)

    suite_id = client.post(
        "/v1/suites",
        json={
            "case_ids": [case["id"] for case in cases],
            "conditions": [{"model": "m"}],
            "budget": {"max_model_calls": 2 * per_cell},
        },
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    # 只有两格被真的启动过，它们各自拿到的额度是「整批剩余」。
    assert [entry["case_id"] for entry in seen] == [case["id"] for case in cases[:2]]
    assert [entry["budget"].max_model_calls for entry in seen] == [2 * per_cell, per_cell]
    assert sum(entry["calls"] for entry in seen) == 2 * per_cell

    # 没轮到的两格：未启动、没有 run_id、没有成因，也没有被算进任何结论。
    for case in cases[2:]:
        item = _item(body, case["id"])
        assert item["status"] == NOT_STARTED
        assert item["run_id"] is None
        assert item["cause"] is None
        assert item["results"] == []

    assert body["status"] == "finished"
    assert body["completed"] == 2
    assert body["counts"]["passed"] == 2
    assert body["counts"]["failed"] == 0
    assert body["counts"]["inconclusive"] == 0
    assert body["counts"]["error"] == 0
    assert body["counts"][NOT_STARTED] == 2

    group = _group(body, model="m")
    # 三个桶互斥且穷尽，「未启动」归 unfinished 一侧。
    assert group["determinable"] == 2
    assert group["undecided"] == 0
    assert group["unfinished"] == 2
    assert group["not_started"] == 2
    assert group["determinable"] + group["undecided"] + group["unfinished"] == group["total"] == 4

    # 记账：已用 / 上限 / 是否触顶 / 有多少格子没跑。
    budget = body["budget"]
    assert budget["max_model_calls"] == 2 * per_cell
    assert budget["max_cost_usd"] is None
    assert budget["model_calls_used"] == 2 * per_cell
    assert budget["exceeded"] is True
    assert budget["stopped_by"] == "model_calls"
    assert budget["not_started"] == 2
    # 假执行器的成本一律记「未知」：未知不是 0。
    assert budget["cost_used_usd"] is None
    assert budget["cost_unknown"] is True
    assert NOT_STARTED_REASON in budget["detail"]
    for banned in BANNED_WORDING:
        assert banned not in budget["detail"]


def test_a_batch_budget_of_zero_starts_nothing_at_all(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限 = 0：一格都不启动，而且批次照样会结束（不留在 pending 里让人一直等）。"""

    cases = _create_cases(client, make_run, count=3)
    seen = _install_spending_runner(monkeypatch, per_cell=3)

    suite_id = client.post(
        "/v1/suites",
        json={
            "case_ids": [case["id"] for case in cases],
            "conditions": [{"model": "m"}],
            "budget": {"max_model_calls": 0},
        },
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    assert seen == [], "上限为 0 时不该有任何格子被启动"
    assert body["status"] == "finished"
    assert body["completed"] == 0
    assert body["counts"][NOT_STARTED] == 3
    for case in cases:
        assert _item(body, case["id"])["status"] == NOT_STARTED

    budget = body["budget"]
    assert budget["model_calls_used"] == 0
    assert budget["exceeded"] is True
    assert budget["stopped_by"] == "model_calls"
    assert budget["not_started"] == 3
    assert NOT_STARTED_LABEL in budget["detail"]


def test_the_last_started_cell_stops_at_its_remaining_allowance(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限不是格子花费的整数倍时，最后启动的那一格在步边界停住，整批依然不超上限。

    这是「整批不超过上限」与「不做预测性中断」两条同时成立时的真实样子：额度分下去，
    格子跑得起多少跑多少，停在哪一步如实记账；后面的格子一格都不会启动。
    """

    per_cell = 3
    allowed = 4
    cases = _create_cases(client, make_run, count=3)
    seen = _install_spending_runner(monkeypatch, per_cell=per_cell)

    suite_id = client.post(
        "/v1/suites",
        json={
            "case_ids": [case["id"] for case in cases],
            "conditions": [{"model": "m"}],
            "budget": {"max_model_calls": allowed},
        },
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    # 第一格拿到全部额度并正常跑完；第二格只剩 1 次，因此它停在步边界。
    assert [entry["budget"].max_model_calls for entry in seen] == [allowed, per_cell - (per_cell - 1)]
    assert [entry["calls"] for entry in seen] == [3, 1]
    assert sum(entry["calls"] for entry in seen) == allowed, "整批实际用量不得超过上限"

    stopped = _item(body, cases[1]["id"])
    assert stopped["status"] == "inconclusive"
    assert stopped["status"] not in ("failed", "error")
    assert stopped["cause"]["code"] == "budget_exceeded"
    assert stopped["run_id"] is not None

    assert _item(body, cases[2]["id"])["status"] == NOT_STARTED
    assert body["counts"]["passed"] == 1
    assert body["counts"]["inconclusive"] == 1
    assert body["counts"][NOT_STARTED] == 1
    assert body["budget"]["model_calls_used"] == allowed
    assert body["budget"]["not_started"] == 1


def test_whichever_dimension_runs_out_first_stops_the_batch(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个维度同时声明：谁先到谁停，两个维度的账都在，谁也不覆盖谁。

    每格要两次调用、每次 $0.5（一格 $1.0）；整批上限是 $1.5 与 100 次调用。第一格跑完
    （$1.0），第二格只剩 $0.5——它跑得起一次、在步边界停下，第三格不再启动。
    """

    per_cell = 2
    cost_per_call = 0.5
    cases = _create_cases(client, make_run, count=3)
    seen = _install_spending_runner(
        monkeypatch, per_cell=per_cell, cost_per_call=cost_per_call
    )

    suite_id = client.post(
        "/v1/suites",
        json={
            "case_ids": [case["id"] for case in cases],
            "conditions": [{"model": "m"}],
            "budget": {"max_cost_usd": 1.5, "max_model_calls": 100},
        },
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    assert [entry["calls"] for entry in seen] == [2, 1]
    assert [entry["budget"].max_cost_usd for entry in seen] == [1.5, 0.5]
    assert [entry["budget"].max_model_calls for entry in seen] == [100, 98]

    assert body["counts"]["passed"] == 1
    assert body["counts"]["inconclusive"] == 1
    assert body["counts"][NOT_STARTED] == 1

    budget = body["budget"]
    # 停的是成本那一维：次数还远没到。两个维度的账都在，没有互相覆盖。
    assert budget["stopped_by"] == "cost"
    assert budget["exceeded"] is True
    assert budget["cost_used_usd"] == 1.5
    assert budget["cost_unknown"] is False
    assert budget["model_calls_used"] == 3
    assert budget["max_model_calls"] == 100
    assert budget["max_cost_usd"] == 1.5
    assert budget["not_started"] == 1
    assert "成本上限" in budget["detail"]


def test_a_batch_that_never_hits_its_cap_is_an_ordinary_batch(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限宽裕时：所有格子都跑，账目照给，但不谎称触顶。"""

    cases = _create_cases(client, make_run, count=2)
    _install_spending_runner(monkeypatch, per_cell=2)

    suite_id = client.post(
        "/v1/suites",
        json={
            "case_ids": [case["id"] for case in cases],
            "conditions": [{"model": "m"}],
            "budget": {"max_model_calls": 100},
        },
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    assert body["completed"] == 2
    assert body["counts"][NOT_STARTED] == 0
    budget = body["budget"]
    assert budget["model_calls_used"] == 4
    assert budget["exceeded"] is False
    assert budget["stopped_by"] is None
    assert budget["not_started"] == 0
    assert NOT_STARTED_REASON not in budget["detail"]


# ------------------------------------------------------------------ 请求校验


def test_the_batch_ledger_lands_before_the_last_cell_becomes_terminal(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整批的账必须在「最后一个格子变成终态」之前落库，否则控制台会读到过期数字。

    批次的可观察状态由格子的状态推导：最后一格一落终态，批次就算「已结束」。如果这时账
    还停在上一格那一刻，用户会读到一个「已结束、但不是整批用量」的记账——写这条能力时
    抓到的正是这个竞态（顺序反了就复现）。因此这里钉的是顺序不变量本身，而不是靠轮询去撞
    那个很窄的窗口：轮询会变成一个时快时慢、说不清红绿的测试。

    记录的是两条真实调用：整批的发布（_publish_suite_budget）与格子的终态落库
    （_finish_item）。变红的方式是确定的：把发布挪到终态之后，最后一条 publish 就会排在
    最后一条终态之后。
    """

    rows = _create_cases(client, make_run, count=2)
    _install_spending_runner(monkeypatch, per_cell=2)
    timeline: list[tuple[str, Any]] = []

    real_finish = suites._finish_item
    real_publish = suites._publish_suite_budget
    real_not_started = suites._mark_not_started

    def finish(item_id: str, verdict: str, results: Any, cause: Any) -> None:
        real_finish(item_id, verdict, results, cause)
        timeline.append(("cell_terminal", item_id))

    def not_started(item_id: str) -> None:
        real_not_started(item_id)
        timeline.append(("cell_terminal", item_id))

    def publish(suite_id: str, ledger: Any) -> None:
        real_publish(suite_id, ledger)
        usage = ledger.usage()
        timeline.append(("batch_published", (usage.model_calls_used, usage.exceeded)))

    monkeypatch.setattr(suites, "_finish_item", finish)
    monkeypatch.setattr(suites, "_publish_suite_budget", publish)
    monkeypatch.setattr(suites, "_mark_not_started", not_started)

    suite_id = client.post(
        "/v1/suites",
        json={
            "case_ids": [row["id"] for row in rows],
            "conditions": [{"model": "m"}],
            "budget": {"max_model_calls": 4},
        },
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    terminal = [index for index, (kind, _) in enumerate(timeline) if kind == "cell_terminal"]
    assert len(terminal) == 2, timeline
    # 关键断言：最后一格变终态的那一刻，账里已经有整批的用量了。
    # （之后再发布几次是幂等的，不算问题；有问题的是「终态可见、账还没到」。）
    before_last_terminal = [
        value
        for index, (kind, value) in enumerate(timeline)
        if kind == "batch_published" and index < terminal[-1]
    ]
    assert before_last_terminal, timeline
    assert max(before_last_terminal) == (4, False)
    assert body["budget"]["model_calls_used"] == 4


def test_a_stopped_batch_publishes_the_stop_before_marking_cells_not_started(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一条不变量走「触顶」那条路：先记下整批已触顶，再让格子变成「未启动」。

    反过来的话，用户会先看到一个「已结束、但没触顶、也没有格子没跑」的批次——那正是把
    一次被预算拦下的批次说成正常跑完。顺序在这里是结论的一部分，不是实现细节。
    """

    rows = _create_cases(client, make_run, count=3)
    _install_spending_runner(monkeypatch, per_cell=2)
    timeline: list[tuple[str, Any]] = []

    real_publish = suites._publish_suite_budget
    real_not_started = suites._mark_not_started

    def not_started(item_id: str) -> None:
        real_not_started(item_id)
        timeline.append(("cell_terminal", item_id))

    def publish(suite_id: str, ledger: Any) -> None:
        real_publish(suite_id, ledger)
        usage = ledger.usage()
        timeline.append(("batch_published", (usage.model_calls_used, usage.exceeded)))

    monkeypatch.setattr(suites, "_publish_suite_budget", publish)
    monkeypatch.setattr(suites, "_mark_not_started", not_started)

    suite_id = client.post(
        "/v1/suites",
        json={
            "case_ids": [row["id"] for row in rows],
            "conditions": [{"model": "m"}],
            "budget": {"max_model_calls": 2},
        },
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    terminal = [index for index, (kind, _) in enumerate(timeline) if kind == "cell_terminal"]
    assert len(terminal) == 2, timeline
    # 第一个「未启动」格子出现之前，整批已经是「已触顶」的状态了。
    before_first_terminal = [
        value
        for index, (kind, value) in enumerate(timeline)
        if kind == "batch_published" and index < terminal[0]
    ]
    assert (2, True) in before_first_terminal, timeline
    assert body["budget"]["exceeded"] is True
    assert body["budget"]["not_started"] == 2


def test_negative_batch_budget_is_rejected(client: Any, make_run: Any) -> None:
    """负数是没意义的上限：与单次回放一样当场 422，而不是跑出一个奇怪的批次。"""

    cases = _create_cases(client, make_run, count=1)
    response = client.post(
        "/v1/suites",
        json={
            "case_ids": [cases[0]["id"]],
            "budget": {"max_model_calls": -1},
        },
    )
    assert response.status_code == 422


def test_an_empty_budget_object_means_no_cap(client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """两个维度都不给 = 不设上限：不声明不等于「上限为 0」，记账对象也不会出现。"""

    cases = _create_cases(client, make_run, count=2)
    _install_plain_runner(monkeypatch)

    suite_id = client.post(
        "/v1/suites",
        json={"case_ids": [case["id"] for case in cases], "budget": {}},
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    assert body["budget"] is None
    assert body["completed"] == 2
    assert body["counts"][NOT_STARTED] == 0


# ------------------------------------------------------------------ 端到端：离线剧本模型的真实产生路径


def _wait_for_parent_run(client: Any, timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for item in client.get("/v1/runs").json()["runs"]:
            run = item["run"]
            if (
                run["parent_run_id"] is None
                and run["agent_name"] == SEED_AGENT
                and run["status"] == "succeeded"
            ):
                return run
        time.sleep(0.2)
    raise AssertionError(f"播种未在 {timeout:.0f}s 内产出可回放的父 Run")


def _wait_for_seeded_case(client: Any, timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cases = client.get("/v1/cases").json()["cases"]
        if cases and cases[0]["last_status"] not in (None, "running"):
            return cases[0]
        time.sleep(0.2)
    raise AssertionError(f"播种未在 {timeout:.0f}s 内产出用例结论")


def _seeded_cases(client: Any, parent_run_id: str, count: int) -> list[dict[str, Any]]:
    """从播种出来的那次真实运行里再建几条用例（回归模式：模型的调用会真的发生）。"""

    return [
        client.post(
            "/v1/cases",
            json={
                "name": f"批量预算用例 {index}",
                "source_run_id": parent_run_id,
                "from_seq": SEED_FROM_SEQ,
                "preset": "regress",
                "assertions": [{"type": "final_output_contains", "value": "REDIS_POOL_SIZE"}],
            },
        ).json()
        for index in range(count)
    ]


def _live_model_calls(client: Any, run_id: str) -> int:
    """数一个回放 Run 里真实发生的模型调用次数（这就是「花了多少」）。"""

    events = client.get(f"/v1/runs/{run_id}/timeline").json()["events"]
    return sum(
        1
        for event in events
        if event["type"] == "model_call" and event["effect_source"] == "live"
    )


def test_a_real_batch_hits_its_cap_and_reports_the_unstarted_cells(seeded_client: Any) -> None:
    """真机：两格的额度里只跑得完第一格，第二格停住、第三格从未启动。

    用的是离线剧本模型（没有任何真实模型调用）。断言的核心是**整批真实调用次数 <= 上限**：
    这个数不是从记账里抄的，而是把每个格子的回放 Run 里 live 的模型调用事件数出来的。
    """

    parent = _wait_for_parent_run(seeded_client)
    cases = _seeded_cases(seeded_client, parent["id"], 3)

    body = seeded_client.post(
        "/v1/suites",
        json={
            "case_ids": [case["id"] for case in cases],
            "conditions": [{}],
            "budget": {"max_model_calls": 2},
        },
    ).json()
    detail = _await_suite(seeded_client, body["suite_id"], timeout=180.0)

    items = _group(detail)["items"]
    started = [item for item in items if item["status"] == NOT_STARTED]
    ran = [item for item in items if item["run_id"]]
    assert [item["case_id"] for item in ran] == [cases[0]["id"]]
    assert [item["case_id"] for item in started] == [cases[1]["id"], cases[2]["id"]]

    # 真实的调用次数：数事件，不数账本。
    spent = sum(_live_model_calls(seeded_client, item["run_id"]) for item in ran)
    assert spent == 2
    assert spent <= detail["budget"]["max_model_calls"]

    assert detail["status"] == "finished"
    assert detail["completed"] == 1
    assert detail["counts"][NOT_STARTED] == 2
    # 那个真的跑完的格子用的是默认 Prompt，结论是「未通过」——它与两个「未启动」分列，
    # 谁也不并进谁：这正是这一轮要守住的那条线。
    assert detail["counts"]["failed"] == 1
    assert detail["counts"]["inconclusive"] == 0
    assert _group(detail)["determinable"] == 1
    assert _group(detail)["undecided"] == 0
    assert _group(detail)["unfinished"] == 2

    budget = detail["budget"]
    assert budget["model_calls_used"] == 2
    assert budget["exceeded"] is True
    assert budget["stopped_by"] == "model_calls"
    assert budget["not_started"] == 2
    # 剧本模型不在价格表内：成本是「未知」，不是 0。
    assert budget["cost_used_usd"] is None
    assert budget["cost_unknown"] is True
    assert "未知" in budget["detail"]
    assert NOT_STARTED_REASON in budget["detail"]

    group = _group(detail)
    assert group["unfinished"] == 2
    assert group["not_started"] == 2
    assert group["undecided"] == 0
    # 触顶时的措辞：不出现「跑完了 / 全部通过 / 拿不到结论」。
    for banned in BANNED_WORDING:
        assert banned not in budget["detail"]


def test_a_real_batch_with_a_zero_cap_starts_no_replay_at_all(seeded_client: Any) -> None:
    """真机：上限 = 0 时一个新回放都不会产生（连一条事件都不该有）。"""

    parent = _wait_for_parent_run(seeded_client)
    # 等播种那条用例也出结论：这样「Run 总数」在提交前后才有可比性。
    _wait_for_seeded_case(seeded_client)
    before = seeded_client.get("/v1/runs").json()["total"]
    cases = _seeded_cases(seeded_client, parent["id"], 2)

    body = seeded_client.post(
        "/v1/suites",
        json={
            "case_ids": [case["id"] for case in cases],
            "conditions": [{}],
            "budget": {"max_model_calls": 0},
        },
    ).json()
    detail = _await_suite(seeded_client, body["suite_id"], timeout=180.0)

    assert detail["completed"] == 0
    assert detail["counts"][NOT_STARTED] == 2
    assert all(item["run_id"] is None for group in detail["groups"] for item in group["items"])
    assert seeded_client.get("/v1/runs").json()["total"] == before
    assert detail["budget"]["model_calls_used"] == 0
    assert detail["budget"]["exceeded"] is True


def test_the_cell_accounting_and_the_batch_accounting_do_not_overwrite_each_other(
    seeded_client: Any,
) -> None:
    """两层记账各自成立：格子的 Run 记自己那一次，批次记整批的合计。

    上一格用掉的部分不会把下一格的账目抹掉，也不会被下一格覆盖——两个数字来自同一个
    账本实现（SDK 的 BudgetLedger），不是把事件再数一遍的第二套口径。
    """

    parent = _wait_for_parent_run(seeded_client)
    cases = _seeded_cases(seeded_client, parent["id"], 2)

    body = seeded_client.post(
        "/v1/suites",
        json={
            "case_ids": [case["id"] for case in cases],
            "conditions": [{}],
            "budget": {"max_model_calls": 4},
        },
    ).json()
    detail = _await_suite(seeded_client, body["suite_id"], timeout=180.0)

    items = _group(detail)["items"]
    assert all(item["run_id"] for item in items)
    assert detail["counts"][NOT_STARTED] == 0

    # 每一格的 Run 都带着自己那一次的记账（上限是它在启动时拿到的「整批剩余」）。
    per_cell: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        run = seeded_client.get(f"/v1/runs/{item['run_id']}").json()
        usage = run["replay"]["budget"]
        assert usage["model_calls_used"] == 2
        # 第一格拿到整批的全部额度，第二格拿到扣掉第一格之后的剩余。
        assert usage["max_model_calls"] == (4 if index == 0 else 2)
        per_cell.append(usage)

    # 批次记的是整批：两格合计 4，且它没有因此声称触顶（账正好用满、后面没有格子了）。
    batch = detail["budget"]
    assert batch["model_calls_used"] == 4
    assert batch["max_model_calls"] == 4
    assert batch["not_started"] == 0
    assert sum(usage["model_calls_used"] for usage in per_cell) == batch["model_calls_used"]


# ------------------------------------------------------------------ 提交前可预估整批


def test_estimate_says_unknown_instead_of_summing_the_known_part(seeded_client: Any) -> None:
    """整批预估：一格给不出成本，整批就不报数字——不把已知的那些加起来冒充整批。"""

    # 只需要「播种完成、用例已就绪」这一个前提：预估本身不碰 Run。
    _wait_for_parent_run(seeded_client)
    case = _wait_for_seeded_case(seeded_client)

    # 复现模式本来就不花模型钱：0 次调用、$0 是事实，不是猜测。
    reproduce = seeded_client.post(
        "/v1/suites/estimate",
        json={"case_ids": [case["id"]], "conditions": [{}]},
    ).json()
    assert reproduce["cells"] == 1
    assert reproduce["model_calls"] == 0
    assert reproduce["cost_usd"] == 0.0
    assert reproduce["cost_is_estimate"] is True

    # 回归模式：分叉点之后两次真实模型调用；剧本模型不在价格表内，因此成本无法预估。
    unpriced = seeded_client.post(
        "/v1/suites/estimate",
        json={"case_ids": [case["id"]], "conditions": [{"model": UNPRICED_MODEL}]},
    ).json()
    assert unpriced["model_calls"] == 2
    assert unpriced["cost_usd"] is None
    assert "无法预估" in unpriced["detail"]

    # 价格表里的模型：成本真的算得出来，且仍然标注为估算。
    priced = seeded_client.post(
        "/v1/suites/estimate",
        json={"case_ids": [case["id"]], "conditions": [{"model": PRICED_MODEL}]},
    ).json()
    assert priced["model_calls"] == 2
    assert priced["cost_usd"] is not None and priced["cost_usd"] > 0

    # 一批里只有一格能定价：整批必须是「无法预估」，而且不等于那个能定价的数。
    mixed = seeded_client.post(
        "/v1/suites/estimate",
        json={
            "case_ids": [case["id"]],
            "conditions": [{"model": PRICED_MODEL}, {"model": UNPRICED_MODEL}],
        },
    ).json()
    assert mixed["cells"] == 2
    assert mixed["model_calls"] == 2 * priced["model_calls"]
    assert mixed["cost_usd"] is None
    assert mixed["cost_usd"] != priced["cost_usd"]
    assert "无法预估" in mixed["detail"]

    # 整批上限会收窄预估的调用量，但不会把未知的成本编成一个数字。
    capped = seeded_client.post(
        "/v1/suites/estimate",
        json={
            "case_ids": [case["id"]],
            "conditions": [{"model": PRICED_MODEL}],
            "budget": {"max_model_calls": 1},
        },
    ).json()
    assert capped["model_calls"] == 1
    assert "上限" in capped["detail"]


#: 预估里不许出现的措辞：它们把「这一维守住了」说成事实，而实际上是两件不同的事
#: （成本未知时这一维根本不会触发；成本已知时已发出的那次调用仍会花钱）。
_NO_GUARANTEE_WORDING = ("不会超过", "最多花", "最多跑这么多", "一定")


def test_estimate_never_promises_a_cost_cap_it_cannot_enforce(seeded_client: Any) -> None:
    """成本未知 + 声明成本上限：如实说「这一维无法判定」，不说「不会超过它」。

    两件事让它没法兑现任何保证（见 docs/replay-semantics.md 10.3）：

    * `BudgetLedger.exceeded_by` 在成本为 None 时**跳过成本判定**，因此这一维根本不会
      触发停止；
    * 即便触发，成本也允许比上限多出最后一次调用（不做预测性中断）。

    这条同时补上了成本上限两个 note 分支的覆盖缺口：旧实现从这里输出一句「成本上限声明为
    $X + 不会超」的保证（本文件禁用的那类措辞），而它直通控制台的「先预估整批」那一行。
    """

    _wait_for_parent_run(seeded_client)
    case = _wait_for_seeded_case(seeded_client)

    def estimate(budget: dict[str, Any]) -> dict[str, Any]:
        response = seeded_client.post(
            "/v1/suites/estimate",
            json={
                "case_ids": [case["id"]],
                "conditions": [{"model": UNPRICED_MODEL}],
                "budget": budget,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    # 剧本模型不在价格表内：成本无法预估。
    only_cost_cap = estimate({"max_cost_usd": 0.000001})
    assert only_cost_cap["cost_usd"] is None
    for banned in _NO_GUARANTEE_WORDING:
        assert banned not in only_cost_cap["detail"], only_cost_cap["detail"]
    # 同时必须说清真实的约束是什么：只有成本上限 + 成本未知 = 这一批没有任何约束。
    assert "无法判定" in only_cost_cap["detail"]
    assert "没有产生实际约束" in only_cost_cap["detail"]

    # 同时声明调用次数上限时，老实说约束落在次数上（成本这一维不参与判定）。
    both = estimate({"max_cost_usd": 0.000001, "max_model_calls": 100})
    for banned in _NO_GUARANTEE_WORDING:
        assert banned not in both["detail"], both["detail"]
    assert "无法判定" in both["detail"]
    assert "调用次数上限" in both["detail"]


def test_estimate_does_not_dress_a_cost_cap_up_as_an_estimate(seeded_client: Any) -> None:
    """成本已知且超过上限时：不把预估改写成上限值，并说清可能多出最后一次调用。

    旧实现会把 `cost_usd` 静默裁成上限，然后写「因此预计最多花这么多」——那等于把
    「到点后不再发起新调用」说成「总花费不超过上限」，而已经发出的那次调用照样要花钱。
    调用次数那一维可以裁（次数在发出之前就知道，是硬上限），成本这一维不行。
    """

    _wait_for_parent_run(seeded_client)
    case = _wait_for_seeded_case(seeded_client)

    priced = seeded_client.post(
        "/v1/suites/estimate",
        json={"case_ids": [case["id"]], "conditions": [{"model": PRICED_MODEL}]},
    ).json()
    assert priced["cost_usd"] is not None and priced["cost_usd"] > 0

    cap = round(priced["cost_usd"] / 4, 6)
    capped = seeded_client.post(
        "/v1/suites/estimate",
        json={
            "case_ids": [case["id"]],
            "conditions": [{"model": PRICED_MODEL}],
            "budget": {"max_cost_usd": cap},
        },
    ).json()

    # 数字没有被静默改写：它仍然是整批的估算，而不是上限值。
    assert capped["cost_usd"] == priced["cost_usd"]
    assert capped["cost_usd"] != cap
    # 上限值本身要在说明里出现（渲染方式与实现同一套插值，不另立一份格式）。
    assert f"整批成本上限是 ${cap}，" in capped["detail"]
    for banned in _NO_GUARANTEE_WORDING:
        assert banned not in capped["detail"], capped["detail"]
    # 如实说明这一维到底会怎样：到点停、已发出的跑完、可能多出最后一次调用。
    assert "步边界" in capped["detail"]
    assert "允许完成" in capped["detail"]
    assert "多出最后一次调用" in capped["detail"]


def test_estimate_is_read_only(seeded_client: Any) -> None:
    """预估不创建批次、也不产生 Run：它只是读录制算一遍。"""

    _wait_for_parent_run(seeded_client)
    _wait_for_seeded_case(seeded_client)
    runs_before = seeded_client.get("/v1/runs").json()["total"]
    suites_before = len(seeded_client.get("/v1/suites").json()["suites"])

    response = seeded_client.post(
        "/v1/suites/estimate",
        json={"case_ids": [], "all_cases": True, "conditions": [{}, {"model": PRICED_MODEL}]},
    )
    assert response.status_code == 200, response.text

    assert seeded_client.get("/v1/runs").json()["total"] == runs_before
    assert len(seeded_client.get("/v1/suites").json()["suites"]) == suites_before


def test_estimate_rejects_an_unknown_case_and_an_empty_target(client: Any) -> None:
    missing = client.post("/v1/suites/estimate", json={"case_ids": ["missing"]})
    assert missing.status_code == 404
    assert "missing" in missing.json()["detail"]

    empty = client.post("/v1/suites/estimate", json={})
    assert empty.status_code == 400


# ------------------------------------------------------------------ 三处闭集与文案


def _ts_values(path: Path, name: str) -> list[str]:
    """从 TypeScript 的联合类型里读出取值，而不是读一份抄写的清单。"""

    source = path.read_text(encoding="utf-8")
    marker = f"export type {name} ="
    assert marker in source, f"{path.name} 里没有找到 {name}"
    block = source[source.index(marker) :]
    # 联合类型可以写成一行（以分号结束），也可以逐行列出（到下一个 export 为止）。
    ends = [index for index in (block.find(";"), block.find("\nexport")) if index != -1]
    block = block[: min(ends)] if ends else block
    return re.findall(r"'([a-z_]+)'", block)


def test_the_cell_status_closed_set_is_the_same_on_all_three_sides() -> None:
    """格子状态的三处定义必须逐字一致，且各层只声明它真的用得上的成员。

    服务端与控制台都覆盖全部七个状态；pi 只管单条用例的结论、没有「格子」，因此它的
    闭集是这七个里的**结论子集**。这条不对等是如实标注：不为了表格好看给 pi 塞一个它
    产生不出来的状态。
    """

    console = _ts_values(ROOT / "web" / "src" / "api" / "types.ts", "SuiteItemStatus")
    pi = _ts_values(ROOT / "integrations" / "pi" / "src" / "core.ts", "Verdict")
    server = list(STATUS_ORDER)

    assert set(console) == set(server)
    assert len(console) == len(set(console))
    assert NOT_STARTED in server and NOT_STARTED in console
    # 「未启动」是未完成态，绝不是结论。
    assert NOT_STARTED not in VERDICTS
    assert NOT_STARTED in OPEN_STATUSES
    # pi 的闭集就是服务端的结论子集，取值逐字一致。
    assert set(pi) == set(VERDICTS)
    assert set(pi) <= set(server)
    assert NOT_STARTED not in pi


def _run_console_module() -> str:
    """真的执行控制台的文案模块，把结果交回 Python。

    web/src/utils/suite.ts 刻意没有任何 import，因此可以被 node 直接执行
    （与 server/tests/test_suites.py 里同一条做法）：抄一份期待值只能证明抄对了。
    """

    if not shutil.which("node"):
        pytest.skip("install node to run the console wording contract")

    script = (
        "import { notStartedLabel, conditionVerdict, unfinishedBreakdown, batchLifecycleLabel } "
        "from './web/src/utils/suite.ts';"
        "let raw = '';"
        "process.stdin.setEncoding('utf8');"
        "process.stdin.on('data', (chunk) => { raw += chunk; });"
        "process.stdin.on('end', () => {"
        "  const payload = JSON.parse(raw);"
        "  console.log(JSON.stringify({"
        "    label: notStartedLabel(),"
        "    stopped: conditionVerdict(payload.stopped),"
        "    finished: conditionVerdict(payload.finished),"
        "    breakdown: unfinishedBreakdown(payload.stopped),"
        "    lifecycle: payload.batches.map((b) => batchLifecycleLabel(b)),"
        "  }));"
        "});"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        input=json.dumps(
            {
                "stopped": {
                    "total": 4,
                    "completed": 1,
                    "determinable": 1,
                    "undecided": 0,
                    "unfinished": 3,
                    "not_started": 3,
                    "determinable_rate": 0.25,
                },
                "finished": {
                    "total": 4,
                    "completed": 4,
                    "determinable": 3,
                    "undecided": 1,
                    "unfinished": 0,
                    "not_started": 0,
                    "determinable_rate": 0.75,
                },
                "batches": [
                    {"status": "running", "exceeded": False, "not_started": 0},
                    {"status": "finished", "exceeded": False, "not_started": 0},
                    # 触顶而停止：还有格子没跑，「已完成」是在把没跑完说成跑完了。
                    {"status": "finished", "exceeded": True, "not_started": 3},
                    # 未启动格子存在（但记账缺失时）也要按「已停止」说。
                    {"status": "finished", "exceeded": False, "not_started": 1},
                ],
            }
        ),
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def test_the_console_and_the_server_say_the_same_thing_about_unstarted_cells() -> None:
    """控制台的说法必须与服务端逐字一致，且触顶时不说「拿不到结论」。"""

    rendered = _run_console_module()

    assert rendered["label"] == NOT_STARTED_LABEL
    assert NOT_STARTED_REASON in rendered["label"]
    # 还有格子没跑完时：只说已完成 / 未完成（并说明其中哪些没启动），
    # 绝不说出「拿不到结论」——那是对没跑过的格子下断言。
    assert rendered["stopped"] == f"共 4 条：已完成 1、未完成 3，其中 3 条{NOT_STARTED_LABEL}"
    for banned in BANNED_WORDING:
        assert banned not in rendered["stopped"]
    # 整批真的跑完时，才允许说「N 条中 M 条拿不到结论」。
    assert rendered["finished"] == "4 条中 1 条拿不到结论"
    # 分组的构成说明与服务端 detail 用的是同一个短语。
    assert rendered["breakdown"] == f"，其中 3 条{NOT_STARTED_LABEL}"


def test_a_stopped_batch_is_never_labelled_as_finished_in_the_console() -> None:
    """触顶而停止的批次角标不许写「已完成」：那是本 PR 新造出的「结束但没跑完」路径。

    「finished」只是生命周期值，它不再等于「所有格子都有结论」。角标若照旧显示「已完成」，
    既紧邻着「已完成 X / Y」的计数（同词两义，违反 utils/suite.ts 自己声明的规则），
    也等于把没跑完说成跑完了——验收 4 明确禁止。这条跑的是控制台的真实模块，因此把
    `batchLifecycleLabel` 改回「finished ⇒ 已完成」会让它变红。
    """

    rendered = _run_console_module()
    assert rendered["lifecycle"] == ["进行中", "已完成", "已停止", "已停止"]
    # 角标那句话里不许出现「已完成」之外的结论性措辞。
    for label in rendered["lifecycle"]:
        assert label in {"进行中", "已完成", "已停止"}
    for banned in BANNED_WORDING:
        assert banned not in rendered["lifecycle"][2]


def test_the_batch_detail_never_claims_a_finished_or_passed_batch() -> None:
    """触顶时的整批说明不含「跑完了 / 全部通过 / 拿不到结论」这类措辞。"""

    from agent_flight_recorder.models import ReplayBudget
    from agent_flight_recorder.replay.budget import BudgetUsage

    usage = BudgetUsage(
        max_model_calls=2,
        model_calls_used=2,
        cost_used_usd=None,
        cost_unknown=True,
        exceeded=True,
        stopped_by="model_calls",
    )
    detail = suites.batch_usage_detail(usage, 3)

    assert NOT_STARTED_LABEL in detail
    assert "未通过" in detail and "无法判断" in detail
    for banned in BANNED_WORDING:
        assert banned not in detail
    # 没触顶、也没有未启动格子时，那句话不会凭空出现。
    assert NOT_STARTED_REASON not in suites.batch_usage_detail(
        usage.model_copy(update={"exceeded": False, "stopped_by": None}), 0
    )
    assert ReplayBudget(max_model_calls=2).is_set
