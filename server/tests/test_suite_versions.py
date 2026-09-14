"""用例集版本（issue #24）：让一批结论**自证它跑的是哪一版用例**。

守四条，每条都能指出一个「改回去就变红」的位置：

1. **归属可查**：套件与每个格子都带着版本标识，按标识能取回**提交那一刻**的定义；
2. **变更可见**：用例被改动后，新批次拿到不同的版本，旧批次既不改标识、也不改定义；
3. **不撒谎**：批次执行期间用例被编辑，整批仍然跑提交那一刻的那一份定义——
   具体到这里是「格子在提交时冻结定义」，所以「删掉冻结、回头读当前行」必红；
4. **内容决定**：哪些差异算「换了定义」、哪些不算，逐条钉死（见 _CANONICALIZATION_RULES）。

最后一条尤其重要：含糊的版本规则比没有版本更糟——它会让「用例变了」这句话既说不清是
什么意思，又不敢信。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

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


def _patch_case(client: Any, case_id: str, **patch: Any) -> dict[str, Any]:
    """改一条用例的定义。改动没落库的话，后面所有断言都会因为「本来就没变」而失去意义，
    所以这里顺手核对一次返回值。"""

    response = client.patch(f"/v1/cases/{case_id}", json=patch)
    assert response.status_code == 200, response.text
    return response.json()


def _submit(client: Any, case_ids: list[str], conditions: list[dict] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"case_ids": case_ids}
    if conditions is not None:
        payload["conditions"] = conditions
    response = client.post("/v1/suites", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _await_suite(client: Any, suite_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/v1/suites/{suite_id}").json()
        if body["status"] == "finished":
            return body
        time.sleep(0.05)
    raise AssertionError(f"批次没有在 {timeout:.0f}s 内结束：{body}")


def _version(client: Any, version_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/case-set-versions/{version_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _frozen_definition(client: Any, version_id: str, case_id: str) -> dict[str, Any]:
    """按版本标识取回某条用例**当时**的定义。找不到就是测试自己要看的信号，不吞掉。"""

    members = _version(client, version_id)["cases"]
    for member in members:
        if member["case_id"] == case_id:
            return member["definition"]
    raise AssertionError(f"这一版里没有这条用例：version={version_id} case={case_id}")


def _await_case(client: Any, case_id: str, timeout: float = 30.0) -> dict[str, Any]:
    """等单条用例的执行落地（HTTP 提交后真正的结论在后台线程里算）。"""

    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/v1/cases/{case_id}").json()
        if body["last_status"] not in (None, "running"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"用例执行没有在 {timeout:.0f}s 内结束：{body}")


def _install_plan_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """把「真的去回放」换成一次成功执行，并记下这次执行**实际用的计划**。

    覆盖是不是真的生效了，唯一的现场就是计划（from_seq / 模型 / Prompt 正文 / 策略）；
    记下它，下面的断言才不是在证明「什么都没发生」。回放之外的链路——覆盖合并、断言求值、
    结论与前提落库——全是真代码。
    """

    from afr_server import cases as cases_module
    from agent_flight_recorder.models import EffectPolicy
    from agent_flight_recorder.replay.engine import ReplayResult

    seen: list[dict[str, Any]] = []

    def execute_replay(plan: Any, run_id: str, *, shared_ledger: Any = None) -> Any:
        seen.append(
            {
                "from_seq": plan.from_seq,
                "model": plan.overrides.model,
                "system_prompt": plan.overrides.system_prompt,
                "model_call_mode": plan.policy.resolve_mode(seq=plan.from_seq, kind="model_call").value,
            }
        )
        return ReplayResult(
            parent_run_id=plan.parent_run_id,
            from_seq=plan.from_seq,
            policy=EffectPolicy.reproduce(),
            run_id=run_id,
            status="succeeded",
        )

    monkeypatch.setattr(cases_module, "execute_replay", execute_replay)
    return seen


def _install_passing_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """假执行器：只保证批次能跑完，结论本身不是这个文件要验证的东西。"""

    from afr_server import suites

    def runner(case_id: str, *, definition: Any = None, on_result: Any = None, **_: Any) -> str:
        if on_result is not None:
            on_result("passed", [{"spec": {"type": "no_error"}, "passed": True, "detail": "假执行器"}], None)
        return "run-fake"

    monkeypatch.setattr(suites, "run_case_blocking", runner)


def _install_replay_result(monkeypatch: pytest.MonkeyPatch, *, status: str = "succeeded") -> None:
    """把「真的去回放」换成一份成功的 ReplayResult，其余全走真代码。

    与上面的假执行器不同：这里 `run_case_blocking` 是真的，因此断言求值、结论落库、
    用例行的执行记账（last_status / last_run_at / last_definition_digest）都是真写下来的。
    要验证「执行记账不参与版本标识」时，这一点是前提。
    """

    from afr_server import cases as cases_module
    from agent_flight_recorder.models import EffectPolicy
    from agent_flight_recorder.replay.engine import ReplayResult

    def execute_replay(plan: Any, run_id: str, *, shared_ledger: Any = None) -> Any:
        return ReplayResult(
            parent_run_id=plan.parent_run_id,
            from_seq=plan.from_seq,
            policy=EffectPolicy.reproduce(),
            run_id=run_id,
            status=status,
        )

    monkeypatch.setattr(cases_module, "execute_replay", execute_replay)


class _ManualExecutor:
    """不自动开跑的假执行器：由测试显式驱动。

    要验证「批次执行期间用例被编辑」这件事，就必须真的占住那个窗口：提交与执行之间
    插入一次编辑。用真的线程池只能靠赛跑，而赛跑赢了不算证据。
    """

    def __init__(self) -> None:
        self._queue: list[tuple[Callable, tuple, dict]] = []

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> None:
        self._queue.append((fn, args, kwargs))

    def pending(self) -> int:
        return len(self._queue)

    def run_next(self) -> None:
        fn, args, kwargs = self._queue.pop(0)
        fn(*args, **kwargs)


def _capture_definitions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """把「真跑一次回放」换成记录，记下这一格**最终按哪一份定义**执行。

    替换点在 cases 的 _execute：回放计划、断言求值、结论落库都真跑完了，被替换掉的只有
    「真的去执行回放」。因此「这一格用的是哪份定义」这个观察点是执行链路上的真值，
    不是 commit 到队列时顺手抄下来的。
    """

    from afr_server import cases as cases_module

    seen: list[dict[str, Any]] = []

    def execute(
        plan: Any,
        run_id: str,
        case_id: str,
        snapshot: dict[str, Any],
        *,
        on_result: Any = None,
        condition: dict[str, Any] | None = None,
        shared_ledger: Any = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        seen.append(
            {
                "case_id": case_id,
                "from_seq": plan.from_seq,
                "assertions": list(snapshot["assertions"]),
                "model": snapshot.get("model"),
                "to_seq": snapshot.get("to_seq"),
            }
        )
        if on_result is not None:
            on_result(
                "passed",
                [{"spec": {"type": "no_error"}, "passed": True, "detail": "记录用的替身"}],
                None,
            )

    monkeypatch.setattr(cases_module, "_execute", execute)
    return seen


# ------------------------------------------------------------------ 归属可查


def test_a_submitted_suite_pins_a_version_that_every_cell_traces_to(client, make_run, monkeypatch) -> None:
    """一次提交，套件与它的每个格子都带着同一个确定版本；版本本身可反查到定义。"""

    _install_passing_runner(monkeypatch)
    first, second = _create_cases(client, make_run, count=2)

    submitted = _submit(client, [first["id"], second["id"]], [{"model": "m1"}, {"model": "m2"}])
    # 归属在提交返回的那一刻就已经定下，不用等批次跑完。
    assert submitted["case_set_version"].startswith("cs1:")

    body = _await_suite(client, submitted["suite_id"])
    case_set = body["case_set"]
    assert case_set["id"] == submitted["case_set_version"]
    assert case_set["canonicalization"] == "cs1"
    assert case_set["case_count"] == 2
    assert case_set["recorded"] is True
    assert case_set["recorded_at"]

    # 每个格子（用例 × 条件，共 4 格）都带着同一个版本标识。
    items = [item for group in body["groups"] for item in group["items"]]
    assert len(items) == 4
    assert {item["case_set_version"] for item in items} == {case_set["id"]}

    # 列表里也看得到：跨批次的比较要建立在这个归属上。
    listed = client.get("/v1/suites").json()["suites"]
    entry = next(item for item in listed if item["id"] == body["id"])
    assert entry["case_set_version"] == case_set["id"]

    # 按标识能取回当时的定义：来源运行、起点、断言、副作用策略都在。
    version = _version(client, case_set["id"])
    assert version["case_count"] == 2
    assert {member["case_id"] for member in version["cases"]} == {first["id"], second["id"]}
    for member in version["cases"]:
        assert member["digest"].startswith("cs1m:")
        definition = member["definition"]
        assert definition["source_run_id"] == "run-1"
        assert definition["from_seq"] == 1
        assert definition["assertions"][0]["type"] == "no_error"
        assert set(definition["effect_policy"]) == {"preset", "policy", "model", "system_prompt"}


def test_the_stored_version_content_recomputes_to_its_id(client, make_run, monkeypatch) -> None:
    """版本行的内容**重算**之后必须等于它的标识，否则「内容决定」只是一句话。

    这条守的是标识与内容之间没有第二条真相：只要有人往版本行里塞了与标识不符的东西
    （或换了个不排序的序列化），这里立刻变红。
    """

    from afr_server.case_versions import case_set_version_id, member_digest

    _install_passing_runner(monkeypatch)
    cases = _create_cases(client, make_run, count=2)
    body = _await_suite(client, _submit(client, [case["id"] for case in cases])["suite_id"])

    version = _version(client, body["case_set"]["id"])
    snapshots = [
        {
            "id": member["case_id"],
            "assertions": member["definition"]["assertions"],
            "source_run_id": member["definition"]["source_run_id"],
            "from_seq": member["definition"]["from_seq"],
            "to_seq": member["definition"]["to_seq"],
            "preset": member["definition"]["effect_policy"]["preset"],
            "policy": member["definition"]["effect_policy"]["policy"],
            "model": member["definition"]["effect_policy"]["model"],
            "system_prompt": member["definition"]["effect_policy"]["system_prompt"],
        }
        for member in version["cases"]
    ]
    assert case_set_version_id(snapshots) == version["id"]
    for member in version["cases"]:
        assert member_digest(member["definition"]) == member["digest"]


def test_two_batches_of_the_same_definitions_share_one_version(client, make_run, monkeypatch) -> None:
    """同一份定义跑两遍：版本相同（因此「同一版的新结论」与「用例变了」可分）。

    这条同时是执行记账（last_*）不在版本里的**反证**：用例行在第一批跑完之后被写上了
    last_status / last_results / last_run_at / last_condition / last_definition_digest，
    如果版本把执行记账算了进去，第二次提交就会拿到一个新的版本号。
    """

    _install_replay_result(monkeypatch)
    cases = _create_cases(client, make_run, count=2)
    ids = [case["id"] for case in cases]

    first = _await_suite(client, _submit(client, ids)["suite_id"])

    # 第一批真的把执行记账写到了用例行上（否则下面那条断言就不是在证明它想证明的事）。
    reread = client.get(f"/v1/cases/{ids[0]}").json()
    assert reread["last_status"] == "passed"
    assert reread["last_run_at"] is not None
    assert reread["last_definition_digest"] is not None

    second = _await_suite(client, _submit(client, ids)["suite_id"])
    assert second["case_set"]["id"] == first["case_set"]["id"]
    # 同一版用例的第二批结论：drift 里一条都不算「变了」。
    assert second["case_set"]["drift"] == {"changed": [], "missing": [], "unchanged": 2}


# ------------------------------------------------------------------ 变更可见


def test_editing_a_case_gives_a_new_version_and_leaves_the_old_one_intact(
    client, make_run, monkeypatch
) -> None:
    """改了断言与起点之后：旧批次纹丝不动，新批次拿到另一个版本，两者不会混成一组数。"""

    _install_passing_runner(monkeypatch)
    first, second = _create_cases(client, make_run, count=2)
    ids = [first["id"], second["id"]]

    before = _await_suite(client, _submit(client, ids)["suite_id"])
    old_version = before["case_set"]["id"]
    frozen = _frozen_definition(client, old_version, first["id"])
    assert frozen["assertions"][0]["type"] == "no_error"
    assert frozen["from_seq"] == 1

    edited = _patch_case(
        client,
        first["id"],
        assertions=[{"type": "tool_called", "tool": "query"}],
        from_seq=3,
    )
    # 改动确实落库了：当前定义摘要换了一个。
    assert edited["definition_digest"] != first["definition_digest"]
    assert edited["from_seq"] == 3

    # 旧批次：版本标识不变，按标识取回的仍然是**当时**的定义。
    again = _await_suite(client, before["id"])
    assert again["case_set"]["id"] == old_version
    assert _frozen_definition(client, old_version, first["id"]) == frozen

    # 用例已经变了：这一批如实标注（归属不受影响——见下一条测试）。
    assert again["case_set"]["drift"] == {
        "changed": [first["id"]],
        "missing": [],
        "unchanged": 1,
    }

    # 新批次拿到**另一个**版本，并且那一版里就是改动后的定义。
    after = _await_suite(client, _submit(client, ids)["suite_id"])
    new_version = after["case_set"]["id"]
    assert new_version != old_version
    changed_definition = _frozen_definition(client, new_version, first["id"])
    assert changed_definition["assertions"][0]["type"] == "tool_called"
    assert changed_definition["from_seq"] == 3
    # 没被改的那条用例两版里是同一份定义：变的是「哪一版」，不是「全都变了」。
    assert _frozen_definition(client, new_version, second["id"]) == _frozen_definition(
        client, old_version, second["id"]
    )

    # 两个批次各自带着自己的版本：没有任何地方把这两组数字并成同一版用例的连续结论。
    fresh = client.get(f"/v1/suites/{before['id']}").json()
    assert fresh["case_set"]["id"] == old_version
    late = client.get(f"/v1/suites/{after['id']}").json()
    assert late["case_set"]["id"] == new_version
    assert {item["case_set_version"] for group in late["groups"] for item in group["items"]} == {
        new_version
    }


def test_a_renamed_case_is_still_the_same_version(client, make_run, monkeypatch) -> None:
    """改名 / 改描述 / 改标签不是「换了一份定义」。

    把展示用的字段算进版本，只会让每一次改名都触发一句「用例变了」——真正需要被看见的
    定义变更会被这种噪音淹掉。
    """

    _install_passing_runner(monkeypatch)
    cases = _create_cases(client, make_run, count=1)
    ids = [case["id"] for case in cases]
    before = _await_suite(client, _submit(client, ids)["suite_id"])

    renamed = _patch_case(
        client,
        ids[0],
        name="改了名字的用例",
        description="改了描述",
        labels={"tier": "p0"},
    )
    assert renamed["name"] == "改了名字的用例"

    after = _await_suite(client, _submit(client, ids)["suite_id"])
    assert after["case_set"]["id"] == before["case_set"]["id"]
    assert after["case_set"]["drift"] == {"changed": [], "missing": [], "unchanged": 1}


def test_a_latest_conclusion_records_which_definition_it_came_from(client, make_run, monkeypatch) -> None:
    """用例页那句「最近一次结论」也要带着定义层面的前提。

    没有它，改完用例之后页面上的结论看起来仍像是在描述当前这份定义——那正是 issue #24
    说的「两个不同定义的运行被读成同一组数字」，只不过发生在单条用例上。
    """

    from afr_server import cases as cases_module

    rows = _create_cases(client, make_run, count=1)
    case_id = rows[0]["id"]

    # 结论由真代码算（断言求值、落库、用例行的记账都在），只有「真的去回放」这一步被换掉。
    _install_replay_result(monkeypatch)
    cases_module.run_case_blocking(case_id)

    after_run = client.get(f"/v1/cases/{case_id}").json()
    assert after_run["last_status"] == "passed"
    # 这次结论是在**这一份**定义下得出的。
    assert after_run["last_definition_digest"] == after_run["definition_digest"]

    # 改掉定义：当前定义与最近一次结论的定义摘要立刻分开，页面才说得清那句话属于谁。
    edited = _patch_case(client, case_id, from_seq=4, assertions=[{"type": "tool_not_called", "tool": "query"}])
    assert edited["definition_digest"] != edited["last_definition_digest"]
    assert edited["last_definition_digest"] == after_run["last_definition_digest"]


# ------------------------------------------------------------------ 不撒谎：批次中途被改


def test_the_batch_runs_the_definition_frozen_at_submit_time(client, make_run, monkeypatch) -> None:
    """批次执行期间用例被编辑：整批仍然跑提交那一刻的定义。

    这是本轮最要紧的一条。做法是占住真正的那个窗口——提交完成、格子还没跑——然后编辑
    用例，再驱动格子执行。「格子在提交时冻结定义」一旦被删掉（改成回头读当前用例行），
    第二格拿到的 from_seq 与断言就会是编辑后的那一份，下面两条断言立刻变红。
    """

    from afr_server import suites as suites_module

    rows = _create_cases(client, make_run, count=1)
    case_id = rows[0]["id"]
    seen = _capture_definitions(monkeypatch)
    executor = _ManualExecutor()
    monkeypatch.setattr(suites_module, "_executor", executor)

    submitted = _submit(client, [case_id], [{"model": "m1"}, {"model": "m2"}])
    version = submitted["case_set_version"]
    body = client.get(f"/v1/suites/{submitted['suite_id']}").json()
    case_ids = body["case_ids"]
    cells = [item for group in body["groups"] for item in group["items"]]
    assert len(cells) == 2 and executor.pending() == 2

    # —— 提交之后、执行之前的那个窗口：改断言、改起点。
    _patch_case(client, case_id, assertions=[{"type": "tool_called", "tool": "query"}], from_seq=7)
    executor.run_next()
    # 再改一次：就算编辑器在手，第二格也不该跟着变。
    _patch_case(client, case_id, assertions=[{"type": "max_tool_calls", "value": 0}], from_seq=9)
    executor.run_next()

    # 编辑确实生效在用例行上（否则下面的断言只是在证明「什么都没发生」）。
    current = client.get(f"/v1/cases/{case_id}").json()
    assert current["from_seq"] == 9
    assert current["assertions"][0]["type"] == "max_tool_calls"

    # 而整批两格跑的都是提交那一刻的那一份：起点、断言、连冻结的用例集合都一致。
    assert [cell["from_seq"] for cell in seen] == [1, 1]
    assert [cell["assertions"][0]["type"] for cell in seen] == ["no_error", "no_error"]
    assert {cell["case_id"] for cell in seen} == set(case_ids)

    # 归属也仍然在提交那一刻：按版本标识取回的还是旧定义（不是现在这份）。
    assert _frozen_definition(client, version, case_id)["from_seq"] == 1
    finished = _await_suite(client, submitted["suite_id"])
    assert finished["case_set"]["id"] == version
    assert {item["case_set_version"] for group in finished["groups"] for item in group["items"]} == {
        version
    }
    # 批次跑完之后，drift 如实说这条用例被改过：结论没变，用例变了。
    assert finished["case_set"]["drift"]["changed"] == [case_id]


def test_a_definition_frozen_at_submit_survives_the_case_being_removed(client, make_run, monkeypatch) -> None:
    """用例行在中途被删掉，格子照样按提交那一刻的定义给出结论。

    冻结定义不只是「防编辑」，也是「不必回头读行」：读不到行就不该有结论这种说法，等于把
    一次已经定下来的判据交给了一个可变的外部状态。"""

    from afr_server import suites as suites_module
    from afr_server.db import session_scope
    from afr_server.storage import get_case
    from afr_server.tables import CaseTable

    rows = _create_cases(client, make_run, count=1)
    case_id = rows[0]["id"]
    seen = _capture_definitions(monkeypatch)
    executor = _ManualExecutor()
    monkeypatch.setattr(suites_module, "_executor", executor)

    submitted = _submit(client, [case_id])
    assert executor.pending() == 1

    with session_scope() as session:
        row = get_case(session, case_id)
        assert row is not None
        session.delete(session.get(CaseTable, case_id))

    executor.run_next()

    assert [cell["from_seq"] for cell in seen] == [1]
    assert seen[0]["assertions"][0]["type"] == "no_error"
    item = client.get(f"/v1/suites/{submitted['suite_id']}").json()["groups"][0]["items"][0]
    assert item["status"] == "passed"
    # 行没了这件事也照实说，不假装它还在。
    drift = client.get(f"/v1/suites/{submitted['suite_id']}").json()["case_set"]["drift"]
    assert drift == {"changed": [], "missing": [case_id], "unchanged": 0}


# ------------------------------------------------------------------ 内容决定的规则


def test_the_version_is_insensitive_to_case_order(client, make_run, monkeypatch) -> None:
    """用例的提交顺序不参与版本标识。

    「这批怎么排」由套件自己记录（case_ids / 格子的 position），「这是哪一版定义」是另一件事——
    同一组用例换个顺序提交，判据一个字都没变。
    """

    _install_passing_runner(monkeypatch)
    first, second = _create_cases(client, make_run, count=2)

    forward = _await_suite(client, _submit(client, [first["id"], second["id"]])["suite_id"])
    backward = _await_suite(client, _submit(client, [second["id"], first["id"]])["suite_id"])

    assert forward["case_set"]["id"] == backward["case_set"]["id"]
    # 顺序本身仍然被记住：两次提交的 case_ids 顺序不同。
    assert forward["case_ids"] == list(reversed(backward["case_ids"]))

    # 少一条用例就是另一版：集合变了，版本必须跟着变。
    subset = _await_suite(client, _submit(client, [first["id"]])["suite_id"])
    assert subset["case_set"]["id"] != forward["case_set"]["id"]


def test_the_canonicalization_rules_are_pinned() -> None:
    """内容决定的规则逐条钉死。含糊的版本规则比没有版本更糟。"""

    from afr_server.case_versions import definition_of, member_digest

    def snapshot(**overrides: Any) -> dict[str, Any]:
        base = {
            "id": "case-1",
            "name": "用例",
            "source_run_id": "run-1",
            "from_seq": 1,
            "to_seq": None,
            "assertions": [{"type": "no_error"}],
            "labels": {},
            "preset": None,
            "policy": None,
            "model": None,
            "system_prompt": None,
        }
        base.update(overrides)
        return base

    baseline = member_digest(definition_of(snapshot()))

    # 1) 字典键序无关。断言那条路靠 AssertionSpec 归一化（键序在解析时就被抹平了），
    #    不在那条路上的嵌套字典（例如回放策略）靠规范化 JSON 排序键——两条都要在。
    reordered = member_digest(
        definition_of(snapshot(assertions=[{"note": "", "value": None, "tool": None, "type": "no_error"}]))
    )
    assert reordered == baseline
    assert member_digest(
        definition_of(snapshot(policy={"default": "dry_run", "by_kind": {"write": "live"}}))
    ) == member_digest(
        definition_of(snapshot(policy={"by_kind": {"write": "live"}, "default": "dry_run"}))
    )

    # 2) 断言的书写形式归一化：省略字段与显式写 null 是同一条断言。
    explicit = member_digest(
        definition_of(
            snapshot(
                assertions=[
                    {"type": "no_error", "value": None, "tool": None, "args_contains": None, "note": ""}
                ]
            )
        )
    )
    assert explicit == baseline

    # 3) 值里的空白是有意义的：断言期望值 "X" 与 " X " 不是一回事，不做 trim。
    padded = member_digest(definition_of(snapshot(assertions=[{"type": "final_output_contains", "value": " X "}])))
    trimmed = member_digest(definition_of(snapshot(assertions=[{"type": "final_output_contains", "value": "X"}])))
    assert padded != trimmed

    # 4) 展示用的字段不参与：改名 / 改描述不产生新版本。
    assert member_digest(definition_of(snapshot(name="换了个名字"))) == baseline
    assert member_digest(definition_of(snapshot(labels={"tier": "p0"}))) == baseline

    # 5) 判据相关的字段参与：起点、终点、来源运行、断言顺序、断言内容、策略。
    for changed in (
        {"from_seq": 2},
        {"to_seq": 3},
        {"source_run_id": "run-2"},
        {"model": "gpt-5-mini"},
        {"preset": "regress"},
        {"system_prompt": "PROMPT V2"},
        {"policy": {"default": "live"}},
        {"assertions": [{"type": "tool_called", "tool": "query"}]},
        {"assertions": [{"type": "no_error"}, {"type": "max_tool_calls", "value": 1}]},
    ):
        assert member_digest(definition_of(snapshot(**changed))) != baseline, f"{changed} 应当改变版本"
    # 断言列表的顺序参与标识：它同时决定断言结果列表的顺序。
    assert member_digest(
        definition_of(
            snapshot(
                assertions=[
                    {"type": "max_tool_calls", "value": 1},
                    {"type": "no_error"},
                ]
            )
        )
    ) != member_digest(
        definition_of(
            snapshot(
                assertions=[
                    {"type": "no_error"},
                    {"type": "max_tool_calls", "value": 1},
                ]
            )
        )
    )

    # 6) 执行记账不参与：否则同一份定义跑第二遍就会「换了定义」。
    assert snapshot(last_status="failed", last_results=[{"passed": False}])
    assert baseline == member_digest(definition_of(snapshot()))


def test_an_assertion_that_cannot_be_parsed_still_gets_a_stable_digest() -> None:
    """存量数据里认不出的断言照原样参与摘要：算不出摘要比摘要不准更糟。

    这条只在函数层验证：一个形态奇怪的旧断言不该让「取回版本」变成 500。
    """

    from afr_server.case_versions import definition_of, member_digest

    weird = {"type": "some_legacy_thing", "payload": [1, 2, {"b": 1, "a": 2}]}
    snapshot = {
        "id": "case-1",
        "assertions": [weird, "甚至不是一个对象"],
        "source_run_id": "run-1",
        "from_seq": 1,
    }
    first = member_digest(definition_of(snapshot))
    assert first == member_digest(definition_of(snapshot))
    # 同一份内容换个键序仍然是同一份。
    assert first == member_digest(definition_of({**snapshot, "assertions": [weird, "甚至不是一个对象"]}))




# ------------------------------------------------------------------ 单次运行的覆盖属于「所用定义」


def test_a_single_run_override_is_attributed_to_the_definition_it_actually_ran(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单条运行的 from_seq 覆盖必须进「所用定义」，否则就是错误归属。

    这是审核在真机上抓到的那条：用例定义在第 15 步，`POST /v1/cases/{id}/run` 带 `from_seq: 1`
    时实际从第 1 步回放，结论却仍被记成第 15 步那一版定义。这里走**同一个 HTTP 入口**把它钉住：
    计划里必须是 1（否则这条测试只是在证明覆盖没生效），记下的前提必须也是 1 那一版。
    """

    from afr_server.case_versions import (
        definition_of,
        effective_snapshot,
        member_digest,
        snapshot_of,
    )
    from afr_server.db import session_scope
    from afr_server.storage import get_case

    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases",
        json={
            "name": "起点覆盖的用例",
            "source_run_id": "run-1",
            "from_seq": 15,
            "assertions": [{"type": "no_error"}],
        },
    ).json()
    assert created["from_seq"] == 15

    seen = _install_plan_recorder(monkeypatch)
    submitted = client.post(f"/v1/cases/{created['id']}/run", json={"from_seq": 1})
    assert submitted.status_code == 200, submitted.text

    after = _await_case(client, created["id"])
    # 覆盖真的生效了：这一次从第 1 步跑起。
    assert [item["from_seq"] for item in seen] == [1]
    assert after["last_status"] == "passed"

    with session_scope() as session:
        base = snapshot_of(get_case(session, created["id"]))
    expected = member_digest(definition_of(effective_snapshot(base, {"from_seq": 1})))
    # 关键断言：记下来的是**有效定义**（第 1 步那一版），不是用例行上那一版（第 15 步）。
    assert after["last_definition_digest"] == expected
    assert after["last_definition_digest"] != after["definition_digest"]
    assert after["definition_digest"] == member_digest(definition_of(base))
    # 覆盖本身也记下来了：只有摘要而没有它，读者只能靠猜两个摘要为什么不同。
    assert after["last_definition_overrides"] == {"from_seq": 1}


def test_every_single_run_override_lands_in_the_premise(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同类入口一并核查：preset / model / system_prompt 覆盖同样进「所用定义」。

    只修 from_seq 是不够的：这几个参数走的是同一条合并路径，漏掉任何一个，都会让「这条结论按
    什么判的」少一块。这里逐项验证，并要求**计划与记录一致**（同一次运行的两个说法）。
    """

    from afr_server.case_versions import (
        definition_of,
        effective_snapshot,
        member_digest,
        snapshot_of,
    )
    from afr_server.db import session_scope
    from afr_server.storage import get_case

    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases",
        json={
            "name": "覆盖各维度的用例",
            "source_run_id": "run-1",
            "assertions": [{"type": "no_error"}],
        },
    ).json()

    seen = _install_plan_recorder(monkeypatch)
    override = {"preset": "regress", "model": "scripted-x", "system_prompt": "PROMPT OVERRIDE"}
    response = client.post(f"/v1/cases/{created['id']}/run", json=override)
    assert response.status_code == 200, response.text

    after = _await_case(client, created["id"])
    # 这三项真的落到了计划上：模型与 Prompt 正文进了覆盖，preset 变成了「模型调用走 live」。
    assert seen[0]["model"] == "scripted-x"
    assert seen[0]["system_prompt"] == "PROMPT OVERRIDE"
    assert seen[0]["model_call_mode"] == "live"

    with session_scope() as session:
        base = snapshot_of(get_case(session, created["id"]))
    # 记录与计划说的是同一件事：有效定义 = 用例定义 ⊕ 这三项覆盖。
    assert after["last_definition_digest"] == member_digest(
        definition_of(effective_snapshot(base, override))
    )
    assert after["last_definition_digest"] != after["definition_digest"]
    assert after["last_definition_overrides"] == override


def test_a_run_without_an_override_records_no_override(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有覆盖时两份摘要相同、覆盖为空：默认路径仍可直接与版本里的成员摘要比对。"""

    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases",
        json={
            "name": "没有覆盖的用例",
            "source_run_id": "run-1",
            "assertions": [{"type": "no_error"}],
        },
    ).json()

    _install_plan_recorder(monkeypatch)
    assert client.post(f"/v1/cases/{created['id']}/run", json={}).status_code == 200
    after = _await_case(client, created["id"])

    assert after["last_definition_digest"] == after["definition_digest"]
    assert after["last_definition_overrides"] is None


def test_budget_is_not_part_of_the_effective_definition(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预算不进「所用定义」：它约束的是跑多少，不是判据（理由写在 protocol 第 7 节）。"""

    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases",
        json={
            "name": "带预算的用例",
            "source_run_id": "run-1",
            "assertions": [{"type": "no_error"}],
        },
    ).json()

    _install_plan_recorder(monkeypatch)
    response = client.post(
        f"/v1/cases/{created['id']}/run", json={"budget": {"max_model_calls": 5}}
    )
    assert response.status_code == 200, response.text
    after = _await_case(client, created["id"])

    assert after["last_definition_overrides"] is None
    assert after["last_definition_digest"] == after["definition_digest"]


def test_an_unknown_override_key_is_rejected() -> None:
    """写错的覆盖键当场报错，而不是留下一个「摘要变了、执行没变」的幽灵差异。"""

    from afr_server.case_versions import effective_snapshot

    assert effective_snapshot({"id": "c"}, {"from_seq": 2})["from_seq"] == 2
    with pytest.raises(ValueError, match="unknown override"):
        effective_snapshot({"id": "c"}, {"fromSeq": 2})



# ------------------------------------------------------------------ 批量格子里的当列条件


def test_a_batch_cell_records_the_condition_it_ran_under_as_an_override(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """批量格子同理：当列条件（模型 / Prompt 正文）是**这次执行的前提**，也进「所用定义」。

    这一条同时钉住两件事不要互相顶替：用例集版本（标识与 drift）记的是**用例定义**；
    用例行上的「最近一次结论」记的是**那次执行的有效定义**（含当列条件）。
    """

    from afr_server.case_versions import (
        definition_of,
        effective_snapshot,
        member_digest,
        snapshot_of,
    )
    from afr_server.db import session_scope
    from afr_server.storage import get_case

    # 真跑链路（含结论落库），只把「真的去回放」换掉：这样用例行的前提才是真写下来的。
    _install_plan_recorder(monkeypatch)
    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases",
        json={
            "name": "带条件的用例",
            "source_run_id": "run-1",
            "assertions": [{"type": "no_error"}],
        },
    ).json()

    suite = _await_suite(
        client,
        _submit(client, [created["id"]], [{"model": "m2"}])["suite_id"],
    )
    # 用例集版本仍然是**用例定义**那一版：条件不进版本（它由格子的 condition 记录）。
    frozen = _frozen_definition(client, suite["case_set"]["id"], created["id"])
    assert frozen["effect_policy"]["model"] is None
    assert suite["case_set"]["drift"] == {"changed": [], "missing": [], "unchanged": 1}

    after = _await_case(client, created["id"])
    with session_scope() as session:
        base = snapshot_of(get_case(session, created["id"]))
    override = {"preset": "regress", "model": "m2"}
    assert after["last_definition_overrides"] == override
    assert after["last_definition_digest"] == member_digest(
        definition_of(effective_snapshot(base, override))
    )
    assert after["last_definition_digest"] != after["definition_digest"]


def test_a_started_run_does_not_keep_the_previous_premise(
    client: Any, make_run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """执行中不挂着上一轮的覆盖：它与摘要同属「整条写下去」的那条记录（protocol 第 7 节）。"""

    from afr_server import background
    from afr_server import cases as cases_module

    client.post("/v1/ingest", json=make_run())
    created = client.post(
        "/v1/cases",
        json={
            "name": "换前提的用例",
            "source_run_id": "run-1",
            "assertions": [{"type": "no_error"}],
        },
    ).json()
    case_id = created["id"]

    _install_plan_recorder(monkeypatch)
    assert client.post(f"/v1/cases/{case_id}/run", json={"from_seq": 1}).status_code == 200
    assert _await_case(client, case_id)["last_definition_overrides"] == {"from_seq": 1}

    # 第二次执行：卡在执行中，好观察「已开始、还没出结论」的那个窗口。
    stalling = threading.Event()
    entered = threading.Event()

    def stall(*args: Any, **kwargs: Any) -> None:
        entered.set()
        stalling.wait(timeout=30)

    monkeypatch.setattr(cases_module, "_execute", stall)
    # 这个 worker 会写库，换库之前必须能被 drain 等到（按 afr- 前缀起名，见 conftest 的诊断网）。
    worker = background.start(
        "afr-test-version-stall-worker", lambda: cases_module.run_case_blocking(case_id)
    )
    try:
        assert entered.wait(timeout=30), "第二次执行没有开始"
        running = client.get(f"/v1/cases/{case_id}").json()
        assert running["last_status"] == "running"
        # 上一轮的覆盖不能粘到这一轮：那会变成「这次还没跑出结论，却已经带着上次的前提」。
        assert running["last_definition_overrides"] is None
        assert running["last_definition_digest"] is None
    finally:
        stalling.set()
        worker.join(timeout=30)


# ------------------------------------------------------------------ 存量数据

def test_a_batch_from_before_versioning_reads_back_as_having_no_version(client, make_run) -> None:
    """版本化之前创建的批次照样读得出来，缺版本时如实显示「无版本记录」。"""

    from afr_server.db import session_scope
    from afr_server.tables import SuiteItemTable, SuiteTable

    rows = _create_cases(client, make_run, count=1)
    case_id = rows[0]["id"]

    # 直接落一行「没有版本」的批次：这就是 _add_missing_columns 给存量行补出来的样子。
    with session_scope() as session:
        session.add(
            SuiteTable(
                id="legacy-suite",
                status="finished",
                case_ids=[case_id],
                conditions=[{"prompt": None, "model": None}],
                case_set_version=None,
            )
        )
        session.add(
            SuiteItemTable(
                id="legacy-item",
                suite_id="legacy-suite",
                case_id=case_id,
                case_name="用例 0",
                position=0,
                condition_key="prompt=|model=",
                condition={"prompt": None, "model": None, "system_prompt": None, "preset": None},
                case_set_version=None,
                status="failed",
                results=[],
            )
        )

    body = client.get("/v1/suites/legacy-suite").json()
    # 无版本记录就是无版本记录：不按当前用例反推一个版本号塞回去。
    assert body["case_set"] is None
    assert body["groups"][0]["items"][0]["case_set_version"] is None
    # 其余的读数一个都不受影响。
    assert body["total"] == 1
    assert body["counts"]["failed"] == 1

    entry = next(item for item in client.get("/v1/suites").json()["suites"] if item["id"] == "legacy-suite")
    assert entry["case_set_version"] is None

    # 存量用例行同样如实：没有记录的字段是 None，而不是被回填一个看起来像真的值。
    case = client.get(f"/v1/cases/{case_id}").json()
    assert case["last_definition_digest"] is None
    # 当前定义的摘要是算出来的，因此存量用例也有——它不依赖任何历史记录。
    assert case["definition_digest"].startswith("cs1m:")

    # 一个不存在的版本标识：找不到就说找不到。
    assert client.get("/v1/case-set-versions/cs1:deadbeef").status_code == 404


def test_the_additive_migration_lands_on_a_database_written_by_the_older_code(
    client, app_env: Path, make_run
) -> None:
    """把库退回版本化之前的样子，再跑一次启动迁移：列被补上、新表被建出、旧行照旧可读。

    「迁移能在既有 data/afr.db 上直接跑通」在单元层的对应物：老代码写下的行（没有这些列）
    必须仍然读得出来，并且读出来是「无版本记录」，而不是一个回填的版本号。
    """

    from afr_server import db
    from afr_server.db import get_engine
    from afr_server.db import session_scope
    from afr_server.tables import SuiteItemTable, SuiteTable

    rows = _create_cases(client, make_run, count=1)
    case_id = rows[0]["id"]
    with session_scope() as session:
        session.add(
            SuiteTable(
                id="pre-versioning",
                status="finished",
                case_ids=[case_id],
                conditions=[{"prompt": None, "model": None}],
                case_set_version="cs1:whatever",
            )
        )
        session.add(
            SuiteItemTable(
                id="pre-versioning-item",
                suite_id="pre-versioning",
                case_id=case_id,
                case_name="用例 0",
                position=0,
                condition_key="prompt=|model=",
                condition={"prompt": None, "model": None, "system_prompt": None, "preset": None},
                case_set_version="cs1:whatever",
                status="passed",
                results=[],
            )
        )
        session.add(
            __import__("afr_server.tables", fromlist=["CaseTable"]).CaseTable(
                id="legacy-case",
                name="存量用例",
                source_run_id="run-1",
                from_seq=1,
                assertions=[{"type": "no_error"}],
                labels={},
                effect_policy={"preset": None, "policy": None, "model": None, "system_prompt": None},
            )
        )

    # 删掉版本化引入的列与表：库里剩下的就是版本化之前的形态（行还在，只是没有这些列）。
    connection = sqlite3.connect(app_env)
    try:
        for table, column in (
            ("suites", "case_set_version"),
            ("suite_items", "case_set_version"),
            ("cases", "last_definition_digest"),
        ):
            connection.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        connection.execute("DROP TABLE case_set_versions")
        connection.commit()
        dropped = {
            table: {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for table in ("cases", "suites", "suite_items")
        }
    finally:
        connection.close()

    # 退掉连接池：这一步是在模拟「另一个进程写下的旧库」，不能让池里那条连接带着旧 schema
    # 的快照继续被复用。
    assert "case_set_version" not in dropped["suites"]
    assert "last_definition_digest" not in dropped["cases"]
    get_engine().dispose()

    # 启动迁移：可重入地补齐可空列，并建出新表。
    db.init_db()
    connection = sqlite3.connect(app_env)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {
            table: {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for table in ("cases", "suites", "suite_items")
        }
    finally:
        connection.close()
    assert "case_set_versions" in tables
    assert "case_set_version" in columns["suites"]
    assert "case_set_version" in columns["suite_items"]
    assert "last_definition_digest" in columns["cases"]

    # 旧行照旧可读：补出来的列是 NULL，因此是「无版本记录」，不是别的什么。
    body = client.get("/v1/suites/pre-versioning").json()
    assert body["case_set"] is None
    assert body["groups"][0]["items"][0]["case_set_version"] is None
    assert body["counts"]["passed"] == 1
    legacy_case = client.get("/v1/cases/legacy-case").json()
    assert legacy_case["last_definition_digest"] is None
    assert legacy_case["definition_digest"].startswith("cs1m:")

    # 迁移之后新批次照旧拿到版本：补出来的列是真的可用，不是只把表结构凑齐。
    from afr_server import suites as suites_module

    def runner(case_id: str, *, definition: Any = None, on_result: Any = None, **_: Any) -> str:
        if on_result is not None:
            on_result("passed", [{"spec": {"type": "no_error"}, "passed": True, "detail": "假执行器"}], None)
        return "run-fake"

    original = suites_module.run_case_blocking
    suites_module.run_case_blocking = runner
    try:
        body = _await_suite(client, _submit(client, [case_id])["suite_id"])
    finally:
        suites_module.run_case_blocking = original
    assert body["case_set"]["recorded"] is True
    assert body["case_set"]["id"].startswith("cs1:")


# ------------------------------------------------------------------ 控制台文案的跨语言契约


def _run_console_module(payload: dict[str, Any]) -> dict[str, Any]:
    """真的执行控制台的文案模块，再把结果交回 Python（与 test_suites.py 同一套做法）。

    web/src/utils/suite.ts 没有任何 import，可以被 node 直接执行（Node 的类型擦除）。
    抄一份期待值只能证明抄对了；执行真模块才能钉住文案本身。
    """

    if not shutil.which("node"):
        pytest.skip("install node to run the console wording contract")

    root = Path(__file__).resolve().parents[2]
    script = (
        "import { caseSetLabel, caseSetDriftNotice, definitionDriftNotice } "
        "from './web/src/utils/suite.ts';"
        "import { describeOverrides } from './web/src/utils/suite.ts';"
        "let raw = '';"
        "process.stdin.setEncoding('utf8');"
        "process.stdin.on('data', (chunk) => { raw += chunk; });"
        "process.stdin.on('end', () => {"
        "  const payload = JSON.parse(raw);"
        "  console.log(JSON.stringify({"
        "    label: caseSetLabel(payload.caseSet),"
        "    drift: caseSetDriftNotice(payload.caseSet),"
        "    definition: definitionDriftNotice(payload.caseItem),"
        "    overrides: describeOverrides(payload.caseItem && payload.caseItem.last_definition_overrides),"
        "  }));"
        "});"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=root,
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def test_the_console_states_the_version_and_the_drift() -> None:
    """控制台说得出「这批跑的是哪一版」与「用例被改过」，且不替缺失的版本编一个。"""

    version_id = "cs1:" + "ab" * 32
    recorded = _run_console_module(
        {
            "caseSet": {
                "id": version_id,
                "canonicalization": "cs1",
                "case_count": 3,
                "recorded": True,
                "recorded_at": "2026-09-15T00:00:00Z",
                "drift": {"changed": [], "missing": [], "unchanged": 3},
            }
        }
    )
    # 版本标识按短形式呈现，但前缀必须还在：读者一眼就能看出这是一个内容决定的标识。
    assert recorded["label"].startswith("用例集版本 cs1:")
    assert "3 条用例" in recorded["label"]
    assert recorded["drift"] == ""
    # 这次调用没有带 caseItem：缺一条用例时该说的话是「没什么可说」，而不是让页面炸掉。
    assert recorded["definition"] == ""

    changed = _run_console_module(
        {
            "caseSet": {
                "id": version_id,
                "canonicalization": "cs1",
                "case_count": 3,
                "recorded": True,
                "recorded_at": "2026-09-15T00:00:00Z",
                "drift": {"changed": ["c1", "c2"], "missing": [], "unchanged": 1},
            }
        }
    )
    # 提示必须同时说清「结论归属不变」——只说「用例变了」会让人以为结论也变了。
    assert "2 条用例" in changed["drift"]
    assert "提交那一刻" in changed["drift"]

    missing = _run_console_module(
        {
            "caseSet": {
                "id": version_id,
                "canonicalization": "cs1",
                "case_count": 2,
                "recorded": True,
                "recorded_at": None,
                "drift": {"changed": [], "missing": ["c9"], "unchanged": 1},
            }
        }
    )
    assert "找不到" in missing["drift"]

    legacy = _run_console_module({"caseSet": None})
    # 无版本记录就是无版本记录：不写「版本：未知」这种像是缺了个默认值的说法。
    assert "无版本记录" in legacy["label"]
    assert legacy["drift"] == ""

    drifted_case = _run_console_module(
        {"caseItem": {"definition_digest": "cs1m:" + "11" * 32, "last_definition_digest": "cs1m:" + "22" * 32}}
    )
    assert "另一版定义" in drifted_case["definition"]
    same = _run_console_module(
        {"caseItem": {"definition_digest": "cs1m:" + "11" * 32, "last_definition_digest": "cs1m:" + "11" * 32}}
    )
    assert same["definition"] == ""
    unrecorded = _run_console_module(
        {"caseItem": {"definition_digest": "cs1m:" + "11" * 32, "last_definition_digest": None}}
    )
    # 没有记录就别说话：拿「没记录」冒充「一致」是这套东西最不该犯的错。
    assert unrecorded["definition"] == ""


def test_the_console_never_calls_an_override_a_definition_change() -> None:
    """带覆盖的那次执行，不许被说成「当前定义已改动」。

    这是审核那条 P1 的同一条道理，只是发生在措辞上：定义可能一个字都没改，差的只是这一次的
    执行覆盖。「当前定义已改动」是一句没有记录支撑的话。
    """

    base = "cs1m:" + "11" * 32
    other = "cs1m:" + "22" * 32

    from_seq_only = _run_console_module(
        {
            "caseItem": {
                "definition_digest": base,
                "last_definition_digest": other,
                "last_definition_overrides": {"from_seq": 1},
            }
        }
    )
    assert "从第 1 步开始" in from_seq_only["definition"]
    assert "当前定义已改动" not in from_seq_only["definition"]
    assert "判据不一致" in from_seq_only["definition"]

    # 覆盖与用例定义是否一致由摘要说：覆盖值恰好等于定义值时，不该凭空多出一句不一致。
    overriding_to_the_same_value = _run_console_module(
        {
            "caseItem": {
                "definition_digest": base,
                "last_definition_digest": base,
                "last_definition_overrides": {"model": "m"},
            }
        }
    )
    assert "模型 m" in overriding_to_the_same_value["definition"]
    assert "判据不一致" not in overriding_to_the_same_value["definition"]

    # 覆盖里没有的东西不许被说出口；空字符串也是**真的覆盖了 Prompt 正文**（不是「没有覆盖」）。
    described = _run_console_module(
        {
            "caseItem": {
                "definition_digest": base,
                "last_definition_digest": other,
                "last_definition_overrides": {"system_prompt": "", "preset": "regress"},
            }
        }
    )
    assert described["overrides"] == "Prompt 正文、回放模式 regress"

    # 没有覆盖时那句话仍然只讲「定义变了」：这一支没有被上面的改动带偏。
    unchanged_definition = _run_console_module(
        {"caseItem": {"definition_digest": base, "last_definition_digest": other}}
    )
    assert "当前定义已改动" in unchanged_definition["definition"]
    assert not unchanged_definition["overrides"]
