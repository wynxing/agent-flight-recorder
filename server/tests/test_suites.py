"""批量套件：一次跑一批用例 × 一组条件，并给出按条件分组的汇总。

这个文件守三件事（issue #10 验收 3/4/5）：

* 每个格子的结论都带着自己的条件，不同 Prompt 版本的结果不混、不丢前提；
* 汇总只有四态计数与「该条件自己的」可判断率，不存在任何跨条件的合计分数；
* inconclusive 与 failed 在聚合层依然分列，成因逐条可见。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
import threading
from typing import Any

import pytest


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


def _install_runner(monkeypatch: pytest.MonkeyPatch, plan: Callable[[str, dict], tuple]) -> list[dict]:
    """用假执行器替换真实回放，结论由 plan 按 (case_id, condition) 决定。

    替换的是 suites 模块里的名字，因此被替换的只有「真跑一次回放」这一步：条件解析、
    格子落库、回调接线、聚合都还是真代码。
    """

    from afr_server import suites

    calls: list[dict[str, Any]] = []

    def runner(
        case_id: str,
        *,
        preset: Any = None,
        model: str | None = None,
        system_prompt: str | None = None,
        on_started: Callable[[str], None] | None = None,
        on_result: Any = None,
        condition: dict[str, Any] | None = None,
    ) -> str:
        calls.append(
            {
                "case_id": case_id,
                "preset": preset,
                "model": model,
                "system_prompt": system_prompt,
                "condition": condition,
            }
        )
        run_id = f"run-{len(calls)}"
        if on_started is not None:
            on_started(run_id)
        if on_result is not None:
            verdict, cause = plan(case_id, condition or {})
            on_result(
                verdict,
                [
                    {
                        "spec": {"type": "no_error"},
                        "passed": verdict == "passed",
                        "detail": f"假执行器给出 {verdict}",
                    }
                ],
                cause,
            )
        return run_id

    monkeypatch.setattr(suites, "run_case_blocking", runner)
    return calls


def _install_presets(monkeypatch: pytest.MonkeyPatch) -> None:
    """给未注册的 demo-agent 装上两版 Prompt，用来验证条件解析与「条件随结果记录」。"""

    from afr_server import suites

    class _Spec:
        prompt_presets = {"v1": "PROMPT V1", "v2": "PROMPT V2"}

    monkeypatch.setattr(suites, "resolve_agent_spec", lambda name: _Spec())


def _await_suite(client: Any, suite_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/v1/suites/{suite_id}").json()
        if body["status"] == "finished":
            return body
        time.sleep(0.05)
    raise AssertionError(f"批次没有在 {timeout:.0f}s 内结束：{body}")


def _group(body: dict[str, Any], prompt: str | None = None, model: str | None = None) -> dict:
    for item in body["groups"]:
        condition = item["condition"]
        if condition.get("prompt") == prompt and condition.get("model") == model:
            return item
    raise AssertionError(f"汇总里没有这个条件：prompt={prompt!r} model={model!r}")


def _failure_cause() -> Any:
    from agent_flight_recorder.replay.reasons import InconclusiveReason

    return InconclusiveReason.from_code("side_effect_blocked", "副作用被闸门拦截，这次执行没有真实发生")


class _ImmediateThreads:
    """把格子放进各自的 daemon 线程里跑。

    直接用 ThreadPoolExecutor 会有个副作用：线程池在解释器退出时会被 join，测试若提前
    失败，那个等超时的线程会把整个测试挂住几十秒。daemon 线程没有这个问题。
    """

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> threading.Thread:
        thread = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
        thread.start()
        return thread


# ------------------------------------------------------------------ 生命周期与矩阵


def test_submit_returns_immediately_with_the_whole_matrix(client, make_run) -> None:
    """提交立刻返回，且返回值里就有分母：总数在执行前就已经确定。"""

    cases = _create_cases(client, make_run, count=2)

    response = client.post(
        "/v1/suites",
        json={
            "case_ids": [case["id"] for case in cases],
            "conditions": [{"model": "scripted-a"}, {"model": "scripted-b"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4

    # 提交时就确定下来的是**分母**。这里刻意不断言「还没跑完」：格子是后台并发跑的，
    # 从提交返回到这次读取之间引擎完全可能已经跑掉几格（这些格子因为没有可重建的
    # Agent 而失败得很快），`status` 与 `completed` 因此都是随进度变化的真值。
    # 一旦把「此刻恰好是 0」写进断言，门禁就会在没有改动任何行为的情况下变红。
    assert body["status"] in {"running", "finished"}

    detail = client.get(f"/v1/suites/{body['suite_id']}").json()
    assert detail["total"] == 4
    assert len(detail["groups"]) == 2
    assert sum(item["total"] for item in detail["groups"]) == 4
    # completed 是真实的计数：落在 0..total 之间，且与四态计数一致（它不是一个分数）。
    assert 0 <= detail["completed"] <= detail["total"]
    assert detail["completed"] == sum(
        detail["counts"][status] for status in ("passed", "failed", "inconclusive", "error")
    )

    # 分母不随执行进度改变：再读一次，四态计数可能变了，总数与分组总数不会。
    later = client.get(f"/v1/suites/{body['suite_id']}").json()
    assert later["total"] == detail["total"] == 4
    assert sum(item["total"] for item in later["groups"]) == 4


def test_batch_finishes_and_states_its_lifecycle(client, make_run, monkeypatch) -> None:
    cases = _create_cases(client, make_run, count=2)
    _install_runner(monkeypatch, lambda case_id, condition: ("passed", None))

    suite_id = client.post(
        "/v1/suites",
        json={"case_ids": [case["id"] for case in cases], "conditions": [{"model": "m"}]},
    ).json()["suite_id"]

    body = _await_suite(client, suite_id)
    assert body["status"] == "finished"
    assert body["completed"] == body["total"] == 2
    assert body["counts"]["passed"] == 2
    assert body["errors"] == 0


def test_all_cases_selects_every_case(client, make_run) -> None:
    _create_cases(client, make_run, count=3)

    body = client.post("/v1/suites", json={"all_cases": True}).json()

    assert body["total"] == 3
    assert _await_suite(client, body["suite_id"])["total"] == 3


def test_each_result_records_the_condition_it_ran_under(client, make_run, monkeypatch) -> None:
    """同一批里两个 Prompt 版本：每条结论都带着自己的条件，且两版都被真的用上。"""

    cases = _create_cases(client, make_run, count=1)
    _install_presets(monkeypatch)
    # 结论由条件决定：这正是「改了 Prompt 到底有没有变好」要能看出来的那件事。
    calls = _install_runner(
        monkeypatch,
        lambda case_id, condition: ("passed" if condition.get("prompt") == "v2" else "failed", None),
    )

    suite_id = client.post(
        "/v1/suites",
        json={
            "case_ids": [cases[0]["id"]],
            "conditions": [{"prompt": "v1"}, {"prompt": "v2"}],
        },
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    # 两版 Prompt 的正文都解析出来了，并且都按回归模式真跑（复现模式换 Prompt 无意义）。
    used = {call["system_prompt"]: call["preset"] for call in calls}
    assert set(used) == {"PROMPT V1", "PROMPT V2"}
    assert all(preset is not None and preset.value == "regress" for preset in used.values())

    # 每个格子都记住自己属于哪个条件，且同一条用例出现在两个条件里。
    first, second = _group(body, prompt="v1"), _group(body, prompt="v2")
    assert first["condition"]["prompt"] == "v1"
    assert second["condition"]["prompt"] == "v2"
    assert first["condition"]["system_prompt"] == "PROMPT V1"
    assert second["condition"]["system_prompt"] == "PROMPT V2"
    assert first["condition_key"] != second["condition_key"]
    assert first["items"][0]["case_id"] == second["items"][0]["case_id"] == cases[0]["id"]
    assert first["counts"]["failed"] == 1
    assert second["counts"]["passed"] == 1


def test_condition_reaches_the_case_row_it_ran_under(client, make_run, monkeypatch) -> None:
    """条件必须一路传到落库这一步。

    用例页上那句「最近一次结论」如果没有前提，就会出现「这条用例上次是什么条件下
    的结论」这种歧义，因此结论、成因、条件三者必须一起落库。
    """

    from afr_server import cases

    rows = _create_cases(client, make_run, count=1)
    stored: list[tuple] = []
    monkeypatch.setattr(cases, "_store", lambda *args: stored.append(args))

    condition = {"prompt": "v1", "model": None, "system_prompt": "PROMPT V1", "preset": "regress"}
    cases.run_case_blocking(rows[0]["id"], condition=condition)

    assert stored, "执行没有落库"
    # 第 6 个位置参数就是条件。这里没有注册可重建的 Agent，因此落的是 error 结论，
    # 也就是说失败路径同样带条件。
    assert stored[0][2] == "error"
    assert stored[0][5] == condition


# ------------------------------------------------------------------ 不合成总分


def test_summary_is_split_by_condition_and_never_composes_a_score(client, make_run, monkeypatch) -> None:
    """审核重点：汇总按条件分别呈现，不存在任何跨条件的单一数字。"""

    cases = _create_cases(client, make_run, count=2)
    good, bad = cases[0]["id"], cases[1]["id"]

    def plan(case_id: str, condition: dict) -> tuple:
        if condition.get("model") == "good":
            return ("passed", None)
        return ("failed", None) if case_id == bad else ("inconclusive", _failure_cause())

    _install_runner(monkeypatch, plan)
    suite_id = client.post(
        "/v1/suites",
        json={
            "case_ids": [good, bad],
            "conditions": [{"model": "good"}, {"model": "bad"}],
        },
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    # 批次层只有计数（四态与总数），没有任何比率、分数或百分制。
    top_level_numbers = {
        key: value
        for key, value in body.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    assert set(top_level_numbers) == {"total", "completed", "errors"}
    for banned in ("score", "composite", "total_score", "pass_rate", "determinable_rate", "rate"):
        assert banned not in body

    # 每个条件各自算：分母是该条件自己的总数，且样本量与比率一起出现。
    good_group, bad_group = _group(body, model="good"), _group(body, model="bad")
    assert good_group["total"] == bad_group["total"] == 2
    assert good_group["determinable"] == 2
    assert good_group["undecided"] == 0
    assert good_group["determinable_rate"] == 1.0
    assert bad_group["determinable"] == 1
    assert bad_group["undecided"] == 1
    assert bad_group["determinable_rate"] == 0.5
    # 两组不同，因此「一个数字概括全部」这种做法在数据上就已经不成立。
    assert good_group["determinable_rate"] != bad_group["determinable_rate"]
    assert good_group["counts"]["passed"] == 2
    assert bad_group["counts"]["failed"] == 1
    assert bad_group["counts"]["inconclusive"] == 1

    # 可判断率的分母永远是该条件自己的 total，而不是全批的总数。
    for group in body["groups"]:
        assert group["determinable"] + group["undecided"] == group["total"]
        assert group["counts"]["passed"] + group["counts"]["failed"] == group["determinable"]


def test_unfinished_cells_are_never_counted_as_undecided(client, make_run, monkeypatch) -> None:
    """「还没跑」与「跑了但拿不到结论」必须是两个数，不能合成一个。

    这条守的是一个**表述缺陷**（issue #10 审核发现）：未完成的格子从未执行过、没有任何
    结论，如果把 total - determinable 直接叫「拿不到结论」，界面在批次进行中就会对一堆
    还没跑过的格子下「拿不到」这个断言——那是「跑过了、但拿不到」才成立的说法。

    因此三个桶互斥且穷尽：total == determinable + undecided + unfinished；
    undecided 只含真的没有可信结论的（inconclusive + error）。
    """

    from afr_server import suites

    rows = _create_cases(client, make_run, count=4)
    # 让最后一格堵住、其余格立刻出结论：这样中途状态是「有的完成了、有的还没完成」，
    # 与真实批次进行中的样子一致。
    stalling_case = rows[-1]["id"]
    released = threading.Event()

    def plan(case_id: str, condition: dict) -> tuple:
        if case_id == stalling_case:
            released.wait(timeout=30)
        return ("passed", None)

    _install_runner(monkeypatch, plan)
    # 每个格子一个 daemon 线程：解释器退出时不会被 join 拖住，测试提前失败也不会卡住。
    monkeypatch.setattr(suites, "_executor", _ImmediateThreads())

    suite_id = client.post(
        "/v1/suites",
        json={"case_ids": [row["id"] for row in rows], "conditions": [{"model": "m"}]},
    ).json()["suite_id"]

    deadline = time.monotonic() + 30
    group: dict[str, Any] = {}
    mid: dict[str, Any] = {}
    while time.monotonic() < deadline:
        mid = client.get(f"/v1/suites/{suite_id}").json()
        group = _group(mid, model="m")
        # 等到「至少一格出结论」且「至少一格还没完成」的那一刻。
        if group["completed"] >= 1 and group["unfinished"] >= 1:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"批次没有进入混合的中途状态：{mid}")

    # 中途：确实同时存在已出结论与未完成的格子。
    assert mid["status"] == "running"
    assert group["unfinished"] >= 1
    assert group["determinable"] >= 1

    # 关键断言：未完成的格子不能被算成「拿不到结论」。
    assert group["undecided"] == group["counts"]["inconclusive"] + group["counts"]["error"]
    assert group["unfinished"] == group["counts"]["pending"] + group["counts"]["running"]
    assert group["undecided"] == 0, "这一批没有任何格子真的拿不到结论"
    # determinable 也不含未完成（它只由 passed + failed 构成）。
    assert group["determinable"] == group["counts"]["passed"] + group["counts"]["failed"]
    assert group["determinable"] + group["undecided"] + group["unfinished"] == group["total"]

    # 未完成的格子上没有成因：成因只属于「跑过了但没有可信结论」的那两类。
    for item in group["items"]:
        assert (item["cause"] is not None) == (item["status"] in {"inconclusive", "error"})

    released.set()
    done = _await_suite(client, suite_id)
    finished = _group(done, model="m")
    assert finished["unfinished"] == 0
    assert finished["determinable"] + finished["undecided"] == finished["total"]


def test_duplicate_conditions_collapse_into_one_column(client, make_run, monkeypatch) -> None:
    """同一个条件提交两次只应得到一列：两列一模一样的结果谁也说不清该看哪一列。"""

    cases = _create_cases(client, make_run, count=1)
    _install_runner(monkeypatch, lambda case_id, condition: ("passed", None))

    body = client.post(
        "/v1/suites",
        json={
            "case_ids": [cases[0]["id"]],
            "conditions": [{"model": "m"}, {"model": "m"}],
        },
    ).json()

    assert body["total"] == 1
    detail = _await_suite(client, body["suite_id"])
    assert len(detail["groups"]) == 1


def test_condition_label_only_states_what_was_actually_requested() -> None:
    """条件标签不能替没覆盖的维度起一个名字。

    没写模型时，每个格子用的是各自用例自己的模型，服务端担保不了某个「默认模型」，
    因此只能说「沿用用例自身」——标签同样适用「只讲证据支持的事」这条标准。
    """

    from afr_server.suites import condition_label

    assert condition_label({}) == "沿用用例自身条件"
    assert condition_label({"prompt": "grounded"}) == "grounded · 沿用用例自身模型"
    assert condition_label({"prompt": "v1", "model": "m"}) == "v1 · m"
    assert condition_label({"model": "m"}) == "沿用用例自身 Prompt · m"

    # 用例页上「最近一次条件」那一行用的是控制台的同一套规则，两侧必须逐字一致，
    # 否则同一条用例在汇总表与用例列表里会显示成两个不同的条件。
    console = _console_labels(
        [{}, {"prompt": "grounded"}, {"prompt": "v1", "model": "m"}, {"model": "m"}]
    )
    assert console == [
        condition_label({}),
        condition_label({"prompt": "grounded"}),
        condition_label({"prompt": "v1", "model": "m"}),
        condition_label({"model": "m"}),
    ]


# ------------------------------------------------------------------ 四态分列与失败可诊断


def test_inconclusive_is_not_merged_into_failed(client, make_run, monkeypatch) -> None:
    cases = _create_cases(client, make_run, count=1)
    plan = {
        "passed": ("passed", None),
        "failed": ("failed", None),
        "blocked": ("inconclusive", _failure_cause()),
    }
    _install_runner(monkeypatch, lambda case_id, condition: plan[condition["model"]])

    suite_id = client.post(
        "/v1/suites",
        json={
            "case_ids": [cases[0]["id"]],
            "conditions": [{"model": "passed"}, {"model": "failed"}, {"model": "blocked"}],
        },
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    assert body["counts"]["passed"] == 1
    assert body["counts"]["failed"] == 1
    assert body["counts"]["inconclusive"] == 1
    assert body["counts"]["error"] == 0

    blocked = _group(body, model="blocked")
    assert blocked["undecided"] == 1
    # 成因随格子给出，且 code 说明它属于「执行没真实发生」，不是「结论是不通过」。
    item = blocked["items"][0]
    assert item["cause"]["code"] == "side_effect_blocked"
    assert item["cause"]["detail"]
    assert item["status"] == "inconclusive"


def test_a_failing_item_does_not_break_the_batch(client, make_run, monkeypatch) -> None:
    """单条用例执行失败不能让整批崩掉，且要能在批次层面看出「有 N 条出错」。"""

    cases = _create_cases(client, make_run, count=3)
    broken = cases[1]["id"]

    def plan(case_id: str, condition: dict) -> tuple:
        if case_id == broken:
            raise RuntimeError("这条用例炸了")
        return ("passed", None)

    _install_runner(monkeypatch, plan)
    suite_id = client.post(
        "/v1/suites",
        json={"case_ids": [case["id"] for case in cases], "conditions": [{"model": "m"}]},
    ).json()["suite_id"]
    body = _await_suite(client, suite_id)

    assert body["status"] == "finished"
    assert body["errors"] == 1
    assert body["counts"]["error"] == 1
    assert body["counts"]["passed"] == 2

    error_item = next(item for item in _group(body, model="m")["items"] if item["case_id"] == broken)
    assert error_item["status"] == "error"
    assert "RuntimeError" in error_item["cause"]["detail"]


# ------------------------------------------------------------------ 请求校验与既有语义


def test_condition_reached_the_latest_run_but_not_a_new_unlabelled_one(
    client, make_run, monkeypatch
) -> None:
    """条件只属于它实际跑过的那一次执行，不会粘到下一次单条运行上。

    单条运行允许直接传 Prompt 正文做覆盖（不带版本名），所以它没有可记的条件名。若不清
    掉上一次的条件，界面就会把这次「没有具名条件」的执行显示成「最近一次条件：grounded」，
    凭空替一次真实覆盖写了一个它没有的前提。docs/protocol.md 第 7 节明文承诺了这条语义。

    观察点是**执行中的那一刻**，而不是结束之后：结论落库时本就会连条件一起写，所以只有
    在「已开始、还没出结论」的窗口里，「沿用上一轮条件」这个缺陷才会露出来——那也正是
    用户会看到它的时刻（卡片上写着「正在执行」）。
    """

    rows = _create_cases(client, make_run, count=1)
    case_id = rows[0]["id"]

    # 带条件跑一次（没有注册可重建的 Agent，因此落的是 error 结论，但条件照样落库）。
    condition = {"prompt": "v1", "model": None, "system_prompt": "PROMPT V1", "preset": "regress"}
    from afr_server import cases as cases_module

    cases_module.run_case_blocking(case_id, condition=condition)
    assert client.get(f"/v1/cases/{case_id}").json()["last_condition"] == condition

    # 第二次执行：卡在执行中，好观察「已开始、还没出结论」的那个窗口。
    stalling = threading.Event()
    entered = threading.Event()

    def stall(*args, **kwargs):
        entered.set()
        stalling.wait(timeout=30)

    monkeypatch.setattr(cases_module, "_execute", stall)
    worker = threading.Thread(
        target=lambda: cases_module.run_case_blocking(case_id), daemon=True
    )
    worker.start()
    try:
        assert entered.wait(timeout=30), "第二次执行没有开始"
        running = client.get(f"/v1/cases/{case_id}").json()
        assert running["last_status"] == "running"
        # 关键断言：执行中不能还挂着上一轮的条件。
        assert running["last_condition"] is None
    finally:
        stalling.set()
        worker.join(timeout=30)

    # 跑完之后也不能凭空补一个条件。
    assert client.get(f"/v1/cases/{case_id}").json()["last_condition"] is None


def test_unknown_case_is_rejected(client, make_run) -> None:
    response = client.post("/v1/suites", json={"case_ids": ["missing"]})
    assert response.status_code == 404
    assert "missing" in response.json()["detail"]


def test_submit_requires_a_target(client) -> None:
    response = client.post("/v1/suites", json={})
    assert response.status_code == 400


def test_unknown_prompt_preset_is_rejected(client, make_run, monkeypatch) -> None:
    """Prompt 版本名打错就当场报错，而不是跑出一个看起来正常的假结论。"""

    cases = _create_cases(client, make_run, count=1)
    _install_presets(monkeypatch)

    response = client.post(
        "/v1/suites",
        json={"case_ids": [cases[0]["id"]], "conditions": [{"prompt": "v9"}]},
    )
    assert response.status_code == 400
    assert "v1" in response.json()["detail"]


def test_single_case_run_path_is_unchanged(client, make_run, monkeypatch) -> None:
    """单条运行路径的返回形状与语义不变：批量没有把它改成另一套东西。"""

    cases = _create_cases(client, make_run, count=1)
    _install_runner(monkeypatch, lambda case_id, condition: ("passed", None))

    suite_id = client.post(
        "/v1/suites",
        json={"case_ids": [cases[0]["id"]], "conditions": [{"model": "m"}]},
    ).json()["suite_id"]
    _await_suite(client, suite_id)

    response = client.post(f"/v1/cases/{cases[0]['id']}/run", json={})
    assert response.status_code == 200
    assert set(response.json()) == {"case_id", "run_id", "status"}

    # 真实执行（这里没有注册可重建的 Agent）仍然落到用例上，路径照旧可用。
    result = _await_case(client, cases[0]["id"])
    assert result["last_status"] in {"failed", "error"}


def _await_case(client: Any, case_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(f"/v1/cases/{case_id}").json()
        if result["last_status"] != "running":
            return result
        time.sleep(0.05)
    raise AssertionError("用例执行没有在预期时间内结束")


# ------------------------------------------------------------------ 真机闭环（播种的真实 Agent）


@pytest.fixture()
def seeded_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """一个真的会播种的客户端。

    与 test_seed.py 里的同名 fixture 一样：conftest 默认关掉播种（否则每个测试都要等
    一次 seed），本用例测的就是真实 Agent 上的批量闭环，因此显式打开。
    """

    monkeypatch.setenv("AFR_DB_PATH", str(tmp_path / "afr-suite-seed.db"))
    monkeypatch.setenv("AFR_SEED_ON_STARTUP", "true")

    from afr_server import config, db

    config.get_settings.cache_clear()
    db.reset_engine()
    db.init_db()

    from fastapi.testclient import TestClient

    from afr_server.main import app

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        db.reset_engine()
        config.get_settings.cache_clear()


def test_matrix_over_the_seeded_case_shows_the_prompt_difference(seeded_client: Any) -> None:
    """一条用例 × 两个 Prompt 版本：界面上能一眼看出哪一个变好了。

    这是本轮的真正验收：默认 Prompt 下这条用例是失败的，改成 grounded 才通过。批次汇总
    必须把这两个结论分列在两个条件组里，而不是给出一个看起来漂亮的总分。
    """

    case = _wait_for_seeded_case(seeded_client)
    assert case["last_status"] == "failed"

    suite_id = seeded_client.post(
        "/v1/suites",
        json={
            "case_ids": [case["id"]],
            "conditions": [{"prompt": "default"}, {"prompt": "grounded"}],
        },
    ).json()["suite_id"]
    body = _await_suite(seeded_client, suite_id, timeout=120.0)

    assert body["status"] == "finished"
    assert body["case_ids"] == [case["id"]]
    assert body["total"] == 2

    baseline = _group(body, prompt="default")
    grounded = _group(body, prompt="grounded")
    assert baseline["counts"]["failed"] == 1
    assert baseline["counts"]["passed"] == 0
    assert grounded["counts"]["passed"] == 1
    assert grounded["counts"]["failed"] == 0

    # 两个条件各自的样本量都是 1，可判断率也都写在旁边，不存在跨条件的合计。
    for group in (baseline, grounded):
        assert group["total"] == 1
        assert group["determinable_rate"] == 1.0
        assert group["items"][0]["case_id"] == case["id"]
        assert group["items"][0]["run_id"]

    # 用例页上的「最近一次结论」也必须带着前提，且与它指向的那次回放同属一个格子。
    reread = seeded_client.get(f"/v1/cases/{case['id']}").json()
    assert reread["last_condition"] is not None
    matching = [
        item
        for group in body["groups"]
        for item in group["items"]
        if item["run_id"] == reread["last_run_id"]
    ]
    assert len(matching) == 1
    assert reread["last_condition"]["prompt"] == matching[0]["condition"]["prompt"]
    assert reread["last_condition"]["system_prompt"] == matching[0]["condition"]["system_prompt"]


def _wait_for_seeded_case(client: Any, timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cases = client.get("/v1/cases").json()["cases"]
        if cases and cases[0]["last_status"] not in (None, "running"):
            return cases[0]
        time.sleep(0.2)
    raise AssertionError(f"播种未在 {timeout:.0f}s 内产出用例结论")


# ------------------------------------------------------------------ 控制台文案的跨语言契约


def _run_console_module(*, groups: Any = None, conditions: Any = None) -> Any:
    """真的执行控制台的文案模块，再把结果交回 Python。

    `web/src/utils/suite.ts` 没有任何 import，可以被 node 直接执行（Node 24 的
    type stripping）。抄一份期待值只能证明抄对了；执行真模块才能钉住文案本身。
    """

    import json
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("install node to run the console wording contract")

    root = Path(__file__).resolve().parents[2]
    script = (
        "import { conditionVerdict, determinableRateText, causeDrillLabel, conditionLabel } "
        "from './web/src/utils/suite.ts';"
        "let raw = '';"
        "process.stdin.setEncoding('utf8');"
        "process.stdin.on('data', (chunk) => { raw += chunk; });"
        "process.stdin.on('end', () => {"
        "  const payload = JSON.parse(raw);"
        "  const groups = payload.groups ?? [];"
        "  const conditions = payload.conditions ?? [];"
        "  console.log(JSON.stringify({"
        "    groups: groups.map((g) => ({"
        "      verdict: conditionVerdict(g), rate: determinableRateText(g),"
        "      drill: causeDrillLabel(g.undecided),"
        "    })),"
        "    labels: conditions.map((c) => conditionLabel(c)),"
        "  }));"
        "});"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=root,
        # 走 stdin 而不是命令行参数：node --eval 下多余参数会落在 argv[1] 而不是 argv[2]，
        # 用 stdin 就没有这层歧义。
        input=json.dumps(
            {"groups": list(groups or []), "conditions": list(conditions or [])}
        ),
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def _console_verdict(progress: dict[str, Any]) -> dict[str, Any]:
    """一个条件的四句文案，全部来自真的执行控制台模块。"""

    return _run_console_module(groups=[progress])["groups"][0]


def _console_labels(conditions: list[dict[str, Any]]) -> list[str]:
    """控制台对一组条件给出的标签。"""

    return _run_console_module(conditions=conditions)["labels"]


def test_console_never_calls_unfinished_cells_undecided() -> None:
    """进行中不得把「还没跑」说成「拿不到结论」，跑完了才允许说。

    这条直接跑控制台的文案函数，因此把模板里的措辞改回收工前那种写法（无论进度都输出
    「N 条中 N 条拿不到结论」）会让它变红。
    """

    # 提交瞬间：一条都还没跑完。
    just_submitted = _console_verdict(
        {
            "total": 5,
            "completed": 0,
            "determinable": 0,
            "undecided": 0,
            "unfinished": 5,
            "determinable_rate": 0.0,
        }
    )
    assert "拿不到结论" not in just_submitted["verdict"]
    assert "未完成 5" in just_submitted["verdict"]
    # 样本量与百分比一起出现，且分母是这条用例自己的总数。
    assert just_submitted["rate"] == "0%（0/5）"

    # 进行中：有格子跑完了，但也还有没跑完的。
    running = _console_verdict(
        {
            "total": 5,
            "completed": 2,
            "determinable": 2,
            "undecided": 0,
            "unfinished": 3,
            "determinable_rate": 0.4,
        }
    )
    assert "拿不到结论" not in running["verdict"]
    assert running["verdict"] == "共 5 条：已完成 2、未完成 3"
    assert running["rate"] == "40%（2/5）"

    # 真的出现「无法判断」但仍有格子没跑完时，也还不允许说「拿不到结论」。
    partial = _console_verdict(
        {
            "total": 4,
            "completed": 3,
            "determinable": 1,
            "undecided": 2,
            "unfinished": 1,
            "determinable_rate": 0.25,
        }
    )
    assert "拿不到结论" not in partial["verdict"]
    assert "未完成 1" in partial["verdict"]

    # 整批跑完：这时才允许说「N 条中 M 条拿不到结论」，M 就是 undecided。
    finished = _console_verdict(
        {
            "total": 4,
            "completed": 4,
            "determinable": 3,
            "undecided": 1,
            "unfinished": 0,
            "determinable_rate": 0.75,
        }
    )
    assert finished["verdict"] == "4 条中 1 条拿不到结论"
    assert finished["rate"] == "75%（3/4）"
    # 下钻入口与汇总句用同一个短语、同一个数。
    assert finished["drill"] == "展开 1 条拿不到结论的成因"
