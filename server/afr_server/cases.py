"""Case 执行：把失败 Run 变成可重复验证的回归用例。

Case 的价值全部落在"能不能被稳定重跑并给出明确结论"上，因此这里做两件事：
按 Case 的 effect policy 回放源 Run，然后用确定性断言给出 pass/fail。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent_flight_recorder.models import EffectPolicy, ReplayPreset, new_id, utcnow

from .assertions import evaluate_assertions
from .db import session_scope
from .replay_runner import build_plan, execute_replay
from .storage import final_output_of, get_case, get_events, update_run_metadata
from .tables import CaseTable

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="afr-case")


def submit_case_run(
    case_id: str,
    *,
    from_seq: int | None = None,
    preset: ReplayPreset | None = None,
    policy: EffectPolicy | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> str:
    with session_scope() as session:
        row = get_case(session, case_id)
        if row is None:
            raise ValueError(f"case not found: {case_id}")
        run_id = new_id()
        row.last_status = "running"
        row.last_run_id = run_id
        row.last_run_at = utcnow()
        row.last_results = []
        session.add(row)

    _executor.submit(_job, case_id, run_id, from_seq, preset, policy, model, system_prompt)
    return run_id


def _job(
    case_id: str,
    run_id: str,
    from_seq: int | None,
    preset: ReplayPreset | None,
    policy: EffectPolicy | None,
    model: str | None,
    system_prompt: str | None,
) -> None:
    try:
        _execute(case_id, run_id, from_seq, preset, policy, model, system_prompt)
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


def _execute(
    case_id: str,
    run_id: str,
    from_seq: int | None,
    preset: ReplayPreset | None,
    policy: EffectPolicy | None,
    model: str | None,
    system_prompt: str | None,
) -> None:
    with session_scope() as session:
        case = get_case(session, case_id)
        if case is None:
            raise ValueError(f"case not found: {case_id}")
        snapshot = snapshot_of(case)

    resolved_from_seq = from_seq or snapshot["from_seq"] or 1
    plan = build_plan(
        snapshot["source_run_id"],
        from_seq=resolved_from_seq,
        preset=preset or _preset(snapshot.get("preset")),
        policy=policy or _policy(snapshot.get("policy")),
        model=model or snapshot.get("model"),
        system_prompt=system_prompt,
        labels={"case_id": case_id, "case_name": snapshot["name"]},
    )

    result = execute_replay(plan, run_id)

    with session_scope() as session:
        events = get_events(session, run_id)
        update_run_metadata(
            session,
            run_id,
            {"afr_case": {"case_id": case_id, "name": snapshot["name"]}},
        )

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

