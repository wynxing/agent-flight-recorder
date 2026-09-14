"""Case 的创建与执行：把失败 Run 变成可重复验证的回归用例。

Case 的价值全部落在"能不能被稳定重跑并给出明确结论"上，因此这里做两件事：
按 Case 的 effect policy 回放源 Run，然后用确定性断言给出 pass/fail。
创建与执行都只有一份实现：HTTP 端点、播种、测试走的是同一条链路。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent_flight_recorder.models import (
    ATTR_GATE,
    EffectPolicy,
    EffectSource,
    ReplayBudget,
    ReplayPreset,
    new_id,
    utcnow,
)
from agent_flight_recorder.replay.engine import ReplayPlan
from agent_flight_recorder.replay.budget import BudgetLedger
from agent_flight_recorder.replay.reasons import (
    InconclusiveCode,
    InconclusiveReason,
    most_significant,
)
from sqlmodel import col, update

from .assertions import AssertionSpec, evaluate_assertions
from .db import session_scope
from .replay_runner import build_plan, execute_replay, prepare_run
from .storage import final_output_of, get_case, get_events, get_run
from .tables import CaseTable

logger = logging.getLogger(__name__)

#: 一次用例执行结束时的观察点：结论、断言结果与成因。
#: 批量套件靠它把每条结论落到自己的格子上；单条执行路径不传它，行为不变。
ResultHook = Callable[[str, list[dict[str, Any]], "InconclusiveReason | None"], None]

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="afr-case")


def write_case_record(session: Any, case_id: str, **record: Any) -> None:
    """把用例行的「最近一次结论」整条写下去。

    这里刻意**不用**「读出行对象 -> 改属性 -> 让 ORM 提交」：那种写法只会提交与
    它读到的那份快照相比有变化的列。批量运行里同一条用例会被多个条件并发执行，
    两个格子都要写这一行，于是「没变动的那几列」会被留在对方写下的值上，拼出
    「A 这次执行的 run_id + B 那次执行的条件」这种自相矛盾的记录——而用例页承诺
    「最近一次条件」与「最近一次执行」属于同一次执行（见 docs/protocol.md 第 7 节）。

    每一条 UPDATE 都带上**这个写入点负责的全部字段**，因此谁最后提交，这一整条
    记录就整个来自谁，不存在按列混搭。
    """

    session.execute(
        update(CaseTable)
        .where(col(CaseTable.id) == case_id)
        .values(**record)
        # 不需要把结果同步回会话里的对象：调用方写完这条就结束，不会再用那个对象。
        .execution_options(synchronize_session=False)
    )


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
    budget: ReplayBudget | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> str:
    """提交一次用例执行，立即返回回放 Run 的 ID。真正的执行在线程池里。"""

    run_id, plan, snapshot = _prepare_case_run(
        case_id,
        from_seq=from_seq,
        preset=preset,
        policy=policy,
        budget=budget,
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
    budget: ReplayBudget | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    shared_ledger: BudgetLedger | None = None,
    on_started: Callable[[str], None] | None = None,
    on_result: ResultHook | None = None,
    condition: dict[str, Any] | None = None,
) -> str:
    """原地执行一次用例，返回回放 Run 的 ID。

    播种与测试需要"函数返回时结论已经落库"，HTTP 路径则继续走线程池。
    两条路径共用同一个 _job，因此语义不会因为入口不同而漂移。

    on_started / on_result 是给批量套件的观察点：格子要在执行前就拿到回放 Run 的
    ID，执行后拿到结论与成因。单条执行路径不传它们，行为与以前逐字一致。

    shared_ledger 也是给批量套件的：整批的账本传下来，这一次执行里真正发生的模型调用
    会同时记进它。单条执行不传，账目只落在自己身上。
    """

    run_id, plan, snapshot = _prepare_case_run(
        case_id,
        from_seq=from_seq,
        preset=preset,
        policy=policy,
        budget=budget,
        model=model,
        system_prompt=system_prompt,
    )
    if on_started is not None:
        on_started(run_id)
    _job(
        plan,
        run_id,
        case_id,
        snapshot,
        on_result=on_result,
        condition=condition,
        shared_ledger=shared_ledger,
    )
    return run_id


def _prepare_case_run(
    case_id: str,
    *,
    from_seq: int | None = None,
    preset: ReplayPreset | None = None,
    policy: EffectPolicy | None = None,
    budget: ReplayBudget | None = None,
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
        # 新一次执行开始时就清掉上一次的条件，与清空断言结果同一个道理：执行中途
        # 停在「正在执行」上的那一刻，不该还挂着上一轮的前提。整条记录一并落下（见
        # write_case_record）：这里写的 run_id 与「结论还没产生」是同一件事的两面，
        # 被拆成两列分别提交就会与别的格子写下的结论拼在一起。
        write_case_record(
            session,
            case_id,
            last_status="running",
            last_run_id=run_id,
            last_run_at=utcnow(),
            last_results=[],
            last_condition=None,
        )

    # 给了新的 system prompt 却没有指定模式时，按回归模式执行。
    # 复现模式完全使用录制结果，模型根本不会跑，换 Prompt 也就没有任何意义。
    # 计划在提交前就确定，回放 Run 也因此可以立刻可见，而不是等第一批事件落库。
    plan = resolve_plan(
        snapshot,
        from_seq=from_seq,
        preset=preset,
        policy=policy,
        # 上限只约束这一次执行。整批的上限不在这里：套件层在启动每一格之前把「整批剩余」
        # 作为这一格的额度分下来（见 suites.py），因此这里拿到的额度天然不会越过整批上限。
        budget=budget,
        model=model,
        system_prompt=system_prompt,
        labels={"case_id": case_id, "case_name": snapshot["name"]},
    )
    prepare_run(plan, run_id, case={"case_id": case_id, "name": snapshot["name"]})
    return run_id, plan, snapshot


def resolve_plan(
    snapshot: dict[str, Any],
    *,
    from_seq: int | None = None,
    preset: ReplayPreset | None = None,
    policy: EffectPolicy | None = None,
    budget: ReplayBudget | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    labels: dict[str, str] | None = None,
) -> ReplayPlan:
    """把「一次用例执行」解析成回放计划。

    执行与整批预估共用这一处：什么时候算回归模式只有这一份判定。两边各写一份的话，
    预估出来的调用量就会与真的跑出来的不是同一件事，而这种偏差在界面上看不出来。
    """

    # 给了新的 system prompt 却没有指定模式时，按回归模式执行。
    # 复现模式完全使用录制结果，模型根本不会跑，换 Prompt 也就没有任何意义。
    resolved_preset = preset or _preset(snapshot.get("preset"))
    if system_prompt is not None and resolved_preset is None:
        resolved_preset = ReplayPreset.REGRESS

    return build_plan(
        snapshot["source_run_id"],
        from_seq=from_seq or snapshot["from_seq"] or 1,
        preset=resolved_preset,
        policy=policy or _policy(snapshot.get("policy")),
        budget=budget,
        model=model or snapshot.get("model"),
        system_prompt=system_prompt if system_prompt is not None else snapshot.get("system_prompt"),
        labels=dict(labels or {}),
    )


def _job(
    plan,
    run_id: str,
    case_id: str,
    snapshot: dict[str, Any],
    *,
    on_result: ResultHook | None = None,
    condition: dict[str, Any] | None = None,
    shared_ledger: BudgetLedger | None = None,
) -> None:
    try:
        if on_result is None and condition is None and shared_ledger is None:
            # 单条执行路径原样调用：有些调用方（含测试替身）只按位置接收参数，
            # 不该因为多了一个批量专用观察点而被迫改签名。
            _execute(plan, run_id, case_id, snapshot)
        else:
            _execute(
                plan,
                run_id,
                case_id,
                snapshot,
                on_result=on_result,
                condition=condition,
                shared_ledger=shared_ledger,
            )
    except Exception as exc:  # noqa: BLE001 - 后台任务失败也要给出结论
        logger.warning("case run failed: %s", exc)
        results = [
            {
                "spec": {"type": "case_execution", "value": None, "note": ""},
                "passed": False,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        ]
        cause = InconclusiveReason.from_code(InconclusiveCode.UNKNOWN.value, f"{type(exc).__name__}: {exc}")
        _store(case_id, run_id, "error", results, cause, condition)
        if on_result is not None:
            on_result("error", results, cause)


def _execute(
    plan,
    run_id: str,
    case_id: str,
    snapshot: dict[str, Any],
    *,
    on_result: ResultHook | None = None,
    condition: dict[str, Any] | None = None,
    shared_ledger: BudgetLedger | None = None,
) -> None:
    # 没有整批账本时不多传参数：单条执行路径的调用方（含测试替身）签名因此一字未改。
    batch_args: dict[str, Any] = {"shared_ledger": shared_ledger} if shared_ledger is not None else {}
    result = execute_replay(plan, run_id, **batch_args)

    with session_scope() as session:
        events = get_events(session, run_id)

    results = evaluate_assertions(snapshot["assertions"], events, final_output_of(events))
    passed = bool(results) and all(item.passed for item in results) and result.status == "succeeded"

    # 两种「无法判断」成因完全不同，必须分开：
    #   * 录制不完整 —— 拿不到可信结论，要去看录制质量；
    #   * 副作用被拦截 —— 这次执行本来就没有真实发生，要去看副作用策略。
    # 以前两者被合并成同一个 inconclusive，控制台无从区分，用户也不知道该看哪一边。
    # 「这次执行没有真实发生」的信号有两个来源，都要认：
    #   * 闸门把写操作/对外动作降级成了 dry_run，并留下 side_effect_gate 标记；
    #   * 策略直接拒绝执行，事件来源被标成 blocked。
    # 用户显式选择的 dry_run 不带闸门标记，因此不会被误算成「被拦截」。
    intercepted = [
        event
        for event in events
        if event.effect_source is EffectSource.BLOCKED
        or (
            event.effect_source is EffectSource.DRY_RUN
            and (event.attributes or {}).get(ATTR_GATE) == ATTR_GATE
        )
    ]
    causes: list[InconclusiveReason] = []
    if not result.complete:
        causes.append(result.reason or InconclusiveReason.from_code(InconclusiveCode.UNKNOWN.value))
    if intercepted:
        causes.append(
            InconclusiveReason.from_code(
                InconclusiveCode.SIDE_EFFECT_BLOCKED.value,
                "副作用被闸门拦截，这次执行没有真实发生："
                + ", ".join(sorted({event.name or "未命名工具" for event in intercepted})),
            )
        )

    cause = most_significant(causes)
    verdict = "inconclusive" if cause is not None else (
        "error" if result.status != "succeeded" else ("passed" if passed else "failed")
    )
    if verdict == "error" and cause is None:
        # 契约：结论不是 passed / failed 时必须非空（docs/protocol.md）。error 的成因
        # 回答的是「为什么没有可信结论」——这次回放本身就没跑成功，与前两类成因都不同，
        # 因此落闭集兜底的 unknown，并把执行状态如实写进 detail。
        cause = InconclusiveReason.from_code(
            InconclusiveCode.UNKNOWN.value,
            f"回放执行本身没有成功（status={result.status}）",
        )
    payload = [item.model_dump(mode="json") for item in results]
    _store(case_id, run_id, verdict, payload, cause, condition)
    if on_result is not None:
        on_result(verdict, payload, cause)


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


def _store(
    case_id: str,
    run_id: str,
    status: str,
    results: list[dict[str, Any]],
    cause: InconclusiveReason | None = None,
    condition: dict[str, Any] | None = None,
) -> None:
    try:
        with session_scope() as session:
            write_case_record(
                session,
                case_id,
                last_status=status,
                last_run_id=run_id,
                last_run_at=utcnow(),
                last_results=results,
                # 结论与成因一起落库：不能只有结论、没有成因。
                last_cause=cause.model_dump(mode="json") if cause else None,
                # 条件也一起落库：同一条用例在不同 Prompt 版本下有不同结论，用例页只显示
                # 结论而不显示前提，就会出现「这条用例上次是什么条件下的结论」这种歧义。
                # 单条运行不带条件时落 None，而不是替它写一句「沿用用例自身条件」：单条
                # 入口允许直接传 Prompt 正文做覆盖（不带版本名），凭空补一个条件名会把一次
                # 真实覆盖描述成「什么都没变」，那比留空更容易误导。
                last_condition=dict(condition) if condition else None,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to persist case result: %s", exc)
