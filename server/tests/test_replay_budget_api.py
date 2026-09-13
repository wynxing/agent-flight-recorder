"""回放预算：可预估、执行硬停、事后如实记账——以及「超限不是失败」。

两类测试各守一件事：

* 判定层：预算触顶的用例结论必须落在 inconclusive 一侧，且成因是 budget_exceeded；
* 端到端：真的跑一次会触顶的回放（离线剧本模型），确认 Run 状态、成因与记账的形状。

剧本模型不在本地价格表内，因此这条链路同时钉住了「成本未知不是 0」。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from agent_flight_recorder.models import RunStatus
from agent_flight_recorder.replay.reasons import InconclusiveReason

#: 离线剧本模型：不在本地价格表内，因此它的成本必须是「未知」而不是 0。
UNPRICED_MODEL = "afr-scripted-sre-v1"
#: 示范 Agent 的名字（examples/langgraph_sre_agent 注册进来的）。
SEED_AGENT = "checkout-api-sre"
#: 与播种用例一致的起点：分叉点之后还有两次真实模型调用（seq 15 与 18）。
SEED_FROM_SEQ = 15


def _wait_for_parent_run(client: Any, timeout: float = 90.0) -> dict[str, Any]:
    """播种跑在后台线程里：等那条已完成、且注册了 Agent 的父 Run 出现。"""

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


def _wait_for_replay(client: Any, run_id: str, timeout: float = 90.0) -> dict[str, Any]:
    """回放跑在线程池里：等它进入终态再读元数据。"""

    deadline = time.time() + timeout
    detail: dict[str, Any] = {}
    while time.time() < deadline:
        detail = client.get(f"/v1/runs/{run_id}").json()
        if detail["run"]["status"] in {"succeeded", "failed", "aborted"}:
            return detail
        time.sleep(0.2)
    raise AssertionError(f"回放未在 {timeout:.0f}s 内结束（当前 {detail.get('run', {}).get('status')}）")


def _wait_for_case(client: Any, timeout: float = 90.0) -> dict[str, Any]:
    """播种出来的那条用例：等它拿到第一次结论（说明父 Run 也已经落库）。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        cases = client.get("/v1/cases").json()["cases"]
        if cases and cases[0]["last_status"] not in (None, "running"):
            return cases[0]
        time.sleep(0.2)
    raise AssertionError(f"播种未在 {timeout:.0f}s 内产出用例结论")


# ---------------------------------------------------------------- 判定层：超限不是失败


def test_budget_stop_lands_on_inconclusive_and_is_never_a_failure(monkeypatch) -> None:
    """因预算停止是第三种结论：判 inconclusive，成因是 budget_exceeded。

    第 2、5 轮各自把同类东西写成 failed / error 返工过一次，这里钉住不许重犯。
    """

    from afr_server import cases

    stored = []
    monkeypatch.setattr(cases, "_store", lambda *args: stored.append(args))
    monkeypatch.setattr(cases, "get_events", lambda *a, **k: [])
    monkeypatch.setattr(cases, "final_output_of", lambda events: "只跑到第 15 步就停了")
    monkeypatch.setattr(
        cases,
        "execute_replay",
        lambda *a: SimpleNamespace(
            status=RunStatus.ABORTED.value,
            complete=False,
            reason=InconclusiveReason.from_code("budget_exceeded", "已用 1 次模型调用（上限 1 次）"),
        ),
    )

    cases._execute(None, "run-1", "case", {"assertions": [{"type": "no_error"}]})

    verdict, cause = stored[0][2], stored[0][4]
    assert verdict == "inconclusive"
    assert verdict not in ("failed", "error")
    assert cause is not None
    assert cause.code == "budget_exceeded"
    # 成因要能说清停在哪，否则用户不知道该把上限调到多少。
    assert "上限" in cause.detail


def test_an_aborted_replay_is_not_reported_as_a_passing_case(monkeypatch) -> None:
    """没跑完绝不能被算成通过：断言全绿也不够，回放本身必须是成功的。"""

    from afr_server import cases

    stored = []
    monkeypatch.setattr(cases, "_store", lambda *args: stored.append(args))
    monkeypatch.setattr(cases, "get_events", lambda *a, **k: [])
    monkeypatch.setattr(cases, "final_output_of", lambda events: "ok")
    monkeypatch.setattr(
        cases,
        "evaluate_assertions",
        lambda *a: [SimpleNamespace(passed=True, model_dump=lambda mode=None: {"passed": True})],
    )
    monkeypatch.setattr(
        cases,
        "execute_replay",
        lambda *a: SimpleNamespace(
            status=RunStatus.ABORTED.value,
            complete=False,
            reason=InconclusiveReason.from_code("budget_exceeded", "已用 1 次模型调用（上限 1 次）"),
        ),
    )

    cases._execute(None, "run-1", "case", {"assertions": [{"type": "no_error"}]})

    assert stored[0][2] == "inconclusive"


# ---------------------------------------------------------------- 端到端：真跑一次会触顶的回放


def test_budget_stop_is_aborted_and_booked_honestly(seeded_client: Any) -> None:
    """设一个会触顶的上限，确认它呈现为「因预算停止」而不是失败，并如实记账。"""

    parent = _wait_for_parent_run(seeded_client)
    response = seeded_client.post(
        f"/v1/runs/{parent['id']}/replay",
        json={"from_seq": SEED_FROM_SEQ, "preset": "regress", "budget": {"max_model_calls": 1}},
    )
    assert response.status_code == 200, response.text
    detail = _wait_for_replay(seeded_client, response.json()["run_id"])

    # 超限不是失败：Run 状态是「已中止」，而且它没有给出结论。
    assert detail["run"]["status"] == RunStatus.ABORTED.value
    assert detail["replay"]["complete"] is False
    assert detail["replay"]["cause"]["code"] == "budget_exceeded"
    assert "上限" in detail["replay"]["cause"]["detail"]

    # 记账：已用 / 上限 / 是否触顶。
    budget = detail["replay"]["budget"]
    assert budget["max_model_calls"] == 1
    assert budget["model_calls_used"] == 1
    assert budget["max_cost_usd"] is None
    assert budget["exceeded"] is True
    assert budget["stopped_by"] == "model_calls"
    assert budget["cost_is_estimate"] is True

    # 成本未知就是「未知」，不能是 0。
    assert budget["cost_used_usd"] is None
    assert budget["cost_unknown"] is True
    assert "未知" in budget["detail"]


def test_estimate_reports_calls_and_says_unknown_instead_of_zero(seeded_client: Any) -> None:
    """提交前可预估：调用量给得出来，成本算不出来时如实说「无法预估」。"""

    parent = _wait_for_parent_run(seeded_client)
    url = f"/v1/runs/{parent['id']}/replay/estimate"

    # 复现模式本来就不花模型钱：0 次调用、$0 是事实。
    reproduce = seeded_client.post(
        url, json={"from_seq": SEED_FROM_SEQ, "preset": "reproduce"}
    ).json()
    assert reproduce["model_calls"] == 0
    assert reproduce["cost_usd"] == 0.0
    assert reproduce["cost_is_estimate"] is True

    # 回归模式：分叉点之后有两次真实模型调用；剧本模型不在价格表内，成本无法预估。
    regress = seeded_client.post(
        url, json={"from_seq": SEED_FROM_SEQ, "preset": "regress"}
    ).json()
    assert regress["model_calls"] == 2
    assert regress["cost_usd"] is None
    assert "无法预估" in regress["detail"]

    # 换成价格表里的模型，成本就真的算得出来，而且仍然标注为估算。
    priced = seeded_client.post(
        url,
        json={"from_seq": SEED_FROM_SEQ, "preset": "regress", "model": "gpt-4o-mini"},
    ).json()
    assert priced["model_calls"] == 2
    assert priced["cost_usd"] is not None
    assert priced["cost_usd"] > 0
    assert priced["cost_is_estimate"] is True


def test_a_replay_without_a_budget_keeps_the_old_shape(seeded_client: Any) -> None:
    """不设上限时行为与以前一致：正常跑完，元数据里不会凭空多出一份账目。"""

    parent = _wait_for_parent_run(seeded_client)
    response = seeded_client.post(
        f"/v1/runs/{parent['id']}/replay",
        json={"from_seq": SEED_FROM_SEQ, "preset": "reproduce"},
    )
    assert response.status_code == 200, response.text
    detail = _wait_for_replay(seeded_client, response.json()["run_id"])

    assert detail["run"]["status"] == RunStatus.SUCCEEDED.value
    assert detail["replay"]["complete"] is True
    assert detail["replay"]["cause"] is None
    assert "budget" not in detail["replay"]


def test_a_case_run_that_hits_its_cap_is_inconclusive_not_failed(seeded_client: Any) -> None:
    """单条用例跑到上限时，结论是「无法判断」而不是「不通过」。

    这是整条链路里最容易写反的一处：用例页面上的红绿只区分 passed / failed，
    把一次没跑完的回放染成红色，等于说这条用例不通过。
    """

    _wait_for_parent_run(seeded_client)
    case = _wait_for_case(seeded_client)

    response = seeded_client.post(
        f"/v1/cases/{case['id']}/run",
        json={
            "system_prompt": "把指标拐点与部署时间对齐，并检查该部署改动的配置项。",
            "budget": {"max_model_calls": 1},
        },
    )
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    _wait_for_replay(seeded_client, run_id)

    deadline = time.time() + 60
    reread: dict[str, Any] = {}
    while time.time() < deadline:
        reread = seeded_client.get(f"/v1/cases/{case['id']}").json()
        if reread["last_run_id"] == run_id and reread["last_status"] != "running":
            break
        time.sleep(0.2)

    assert reread["last_run_id"] == run_id
    assert reread["last_status"] == "inconclusive"
    assert reread["last_status"] not in ("failed", "error")
    assert reread["last_cause"]["code"] == "budget_exceeded"

