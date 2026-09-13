"""Case 的创建与执行：把失败 Run 变成可重复验证的回归用例。

Case 的价值全部落在"能不能被稳定重跑并给出明确结论"上，因此这里做两件事：
按 Case 的 effect policy 回放源 Run，然后用确定性断言给出 pass/fail。
创建与执行都只有一份实现：HTTP 端点、播种、测试走的是同一条链路。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent_flight_recorder.models import EffectPolicy, ReplayPreset, new_id, utcnow
from agent_flight_recorder.replay.engine import ReplayPlan

from .assertions import AssertionSpec, evaluate_assertions
from .db import session_scope
from .replay_runner import build_plan, execute_replay, prepare_run
from .storage import final_output_of, get_case, get_events, get_run
from .tables import CaseTable

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="afr-case")


def create_case(
    session: Any,
    *,
    name: str,
    source_run_id: str,
    assertions: Sequence[AssertionSpec | dict[str, Any]] = (),
    description: str = "",
    from_seq: int | None = None,
    to_seq: int | None = None,
    labels: dict[str, str] | None = None,
    preset: ReplayPreset | None = None,
    policy: EffectPolicy | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> CaseTable:
    """建一条 Case 并返回落库的行。

    端点与播种共用这里："从界面创建"和"Agent 自带"必须是同一种东西，
    否则两套建行逻辑会慢慢漂移出不同的默认值。
    """

    if get_run(session, source_run_id) is None:
        raise ValueError(f"run not found: {source_run_id}")

    row = CaseTable(
        id=new_id(),
        name=name,
        description=description,
        source_run_id=source_run_id,
        from_seq=from_seq if from_seq is not None else 1,
        to_seq=to_seq,
        assertions=[_assertion_dict(item) for item in assertions],
        labels=dict(labels or {}),
        effect_policy={
            "preset": preset.value if preset else None,
            "policy": policy.model_dump(mode="json") if policy else None,
            "model": model,
            "system_prompt": system_prompt,
        },
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _assertion_dict(item: AssertionSpec | dict[str, Any]) -> dict[str, Any]:
    """断言可能是模型也可能是原始 dict，落库统一成 JSON。"""

    if isinstance(item, AssertionSpec):
        return item.model_dump(mode="json")
    return dict(item)


def submit_case_run(
    case_id: str,
    *,
    from_seq: int | None = None,
    preset: ReplayPreset | None = None,
    policy: EffectPolicy | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> str:
    """提交一次用例执行，立即返回回放 Run 的 ID。真正的执行在线程池里。"""

    run_id, plan, snapshot = _prepare_case_run(
        case_id,
        from_seq=from_seq,
        preset=preset,
        policy=policy,
        model=model,
        system_prompt=system_prompt,
    )
    _executor.submit(_job, plan, run_id, case_id, snapshot)
    return run_id


def run_case_blocking(
    case_id: str,
    *,
    from_seq: int | None = None,
    preset: ReplayPreset | None = None,
    policy: EffectPolicy | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> str:
    """原地执行一次用例，返回回放 Run 的 ID。

    播种与测试需要"函数返回时结论已经落库"，HTTP 路径则继续走线程池。
    两条路径共用同一个 _job，因此语义不会因为入口不同而漂移。
    """

    run_id, plan, snapshot = _prepare_case_run(
        case_id,
        from_seq=from_seq,
        preset=preset,
        policy=policy,
        model=model,
        system_prompt=system_prompt,
    )
    _job(plan, run_id, case_id, snapshot)
    return run_id


def _prepare_case_run(
    case_id: str,
    *,
    from_seq: int | None = None,
    preset: ReplayPreset | None = None,
    policy: EffectPolicy | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> tuple[str, ReplayPlan, dict[str, Any]]:
    """准备好一次用例执行：占住 Case 状态、定下回放计划、先把 Run 行建出来。"""

    with session_scope() as session:
        row = get_case(session, case_id)
        if row is None:
            raise ValueError(f"case not found: {case_id}")
        snapshot = snapshot_of(row)
        run_id = new_id()
        row.last_status = "running"
        row.last_run_id = run_id
        row.last_run_at = utcnow()
        row.last_results = []
        session.add(row)

    # 给了新的 system prompt 却没有指定模式时，按回归模式执行。
    # 复现模式完全使用录制结果，模型根本不会跑，换 Prompt 也就没有任何意义。
    resolved_preset = preset or _preset(snapshot.get("preset"))
    if system_prompt is not None and resolved_preset is None:
        resolved_preset = ReplayPreset.REGRESS

    # 计划在提交前就确定，回放 Run 也因此可以立刻可见，而不是等第一批事件落库。
    plan = build_plan(
        snapshot["source_run_id"],
        from_seq=from_seq or snapshot["from_seq"] or 1,
        preset=resolved_preset,
        policy=policy or _policy(snapshot.get("policy")),
        model=model or snapshot.get("model"),
        system_prompt=system_prompt if system_prompt is not None else snapshot.get("system_prompt"),
        labels={"case_id": case_id, "case_name": snapshot["name"]},
    )
    prepare_run(plan, run_id, case={"case_id": case_id, "name": snapshot["name"]})
    return run_id, plan, snapshot


def _job(
    plan,
    run_id: str,
    case_id: str,
    snapshot: dict[str, Any],
) -> None:
    try:
        _execute(plan, run_id, case_id, snapshot)
    except Exception as exc:  # noqa: BLE001 - 后台任务失败也要给出结论
        logger.warning("case run failed: %s", exc)
        _store(
            case_id,
            run_id,
            "error",
            [
                {
                    "spec": {"type": "case_execution", "value": None, "note": ""},
                    "passed": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            ],
        )


def _execute(plan, run_id: str, case_id: str, snapshot: dict[str, Any]) -> None:
    result = execute_replay(plan, run_id)

    with session_scope() as session:
        events = get_events(session, run_id)

    results = evaluate_assertions(snapshot["assertions"], events, final_output_of(events))
    passed = bool(results) and all(item.passed for item in results) and result.status == "succeeded"
    _store(
        case_id,
        run_id,
        "passed" if passed else "failed",
        [item.model_dump(mode="json") for item in results],
    )


def snapshot_of(case: CaseTable) -> dict[str, Any]:
    policy = case.effect_policy or {}
    return {
        "id": case.id,
        "name": case.name,
        "source_run_id": case.source_run_id,
        "from_seq": case.from_seq,
        "to_seq": case.to_seq,
        "assertions": list(case.assertions or []),
        "labels": dict(case.labels or {}),
        "preset": policy.get("preset"),
        "policy": policy.get("policy"),
        "model": policy.get("model"),
        "system_prompt": policy.get("system_prompt"),
    }


def _preset(value: Any) -> ReplayPreset | None:
    if value is None:
        return None
    try:
        return ReplayPreset(str(value))
    except ValueError:
        return None


def _policy(value: Any) -> EffectPolicy | None:
    return EffectPolicy.model_validate(value) if value else None


def _store(case_id: str, run_id: str, status: str, results: list[dict[str, Any]]) -> None:
    try:
        with session_scope() as session:
            row = get_case(session, case_id)
            if row is None:
                return
            row.last_status = status
            row.last_run_id = run_id
            row.last_run_at = utcnow()
            row.last_results = results
            session.add(row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to persist case result: %s", exc)
