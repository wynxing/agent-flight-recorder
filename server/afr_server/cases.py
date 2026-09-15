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
from .case_versions import (
    definition_digest_of,
    effective_snapshot,
    normalized,
    run_overrides,
    snapshot_of,
)
from .db import bound_db_path, session_scope
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


#: 可以直接改写的标量字段。其余字段要么有解析过程（断言 / 策略），要么要校验存在性
#: （source_run_id），各自在下面对应分支里处理。
_PATCHABLE_SCALARS: tuple[str, ...] = ("name", "description", "from_seq", "to_seq")
#: effect_policy 这一列里的四个键（见 create_case）。给一个就只改一个。
_POLICY_KEYS: tuple[str, ...] = ("preset", "policy", "model", "system_prompt")


def update_case(session: Any, case_id: str, *, patch: dict[str, Any]) -> CaseTable:
    """改一条用例的定义。**只改给出的字段**，其余原样保留。

    issue #24 需要「用例会被改动」这件事在真实闭环里可发生：在此之前用例没有任何写路径，
    于是「历史结论归属哪一版定义」既触发不了、也验证不了。这里刻意保持最小：改的是定义
    本身，不改任何一条已经落库的结论（结论的归属由 case_versions 固定）。
    """

    row = get_case(session, case_id)
    if row is None:
        raise ValueError(f"case not found: {case_id}")

    for name in _PATCHABLE_SCALARS:
        if name in patch:
            setattr(row, name, patch[name])

    if "source_run_id" in patch:
        source_run_id = patch["source_run_id"]
        if get_run(session, str(source_run_id)) is None:
            raise ValueError(f"run not found: {source_run_id}")
        row.source_run_id = str(source_run_id)

    if "assertions" in patch:
        row.assertions = [_assertion_dict(item) for item in (patch["assertions"] or [])]

    if "labels" in patch:
        row.labels = dict(patch["labels"] or {})

    if any(name in patch for name in _POLICY_KEYS):
        # 整列重新赋值而不是就地改字典：JSON 列的变更检测靠赋值，就地改键不会被提交。
        policy = dict(row.effect_policy or {})
        for name in _POLICY_KEYS:
            if name not in patch:
                continue
            value = patch[name]
            if name == "preset" and value is not None and not isinstance(value, str):
                value = value.value
            if name == "policy" and value is not None and not isinstance(value, dict):
                value = value.model_dump(mode="json")
            policy[name] = value
        row.effect_policy = policy

    session.add(row)
    session.flush()
    return row


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

    run_id, plan, snapshot, overrides = _prepare_case_run(
        case_id,
        from_seq=from_seq,
        preset=preset,
        policy=policy,
        budget=budget,
        model=model,
        system_prompt=system_prompt,
    )
    _executor.submit(_job, plan, run_id, case_id, snapshot, overrides=overrides)
    return run_id


def run_case_blocking(
    case_id: str,
    *,
    definition: dict[str, Any] | None = None,
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

    definition 也是给批量套件的：提交那一刻冻结的用例定义。传了它就不再读用例行——
    批次执行期间用例被编辑，前半批与后半批也仍然跑同一份定义（见 suites.submit_suite）。
    单条执行不传，行为与以前逐字一致：读当前行。

    shared_ledger 也是给批量套件的：整批的账本传下来，这一次执行里真正发生的模型调用
    会同时记进它。单条执行不传，账目只落在自己身上。
    """

    run_id, plan, snapshot, overrides = _prepare_case_run(
        case_id,
        definition=definition,
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
        overrides=overrides,
    )
    return run_id


def _prepare_case_run(
    case_id: str,
    *,
    definition: dict[str, Any] | None = None,
    from_seq: int | None = None,
    preset: ReplayPreset | None = None,
    policy: EffectPolicy | None = None,
    budget: ReplayBudget | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> tuple[str, ReplayPlan, dict[str, Any], dict[str, Any]]:
    """准备好一次用例执行：占住 Case 状态、定下回放计划、先把 Run 行建出来。

    definition 给了就用它（批次提交那一刻冻结的定义），没给才读用例行。

    这里把**覆盖**合并一次（`effective_snapshot`），并把合并结果交给计划与记录两处：
    「按什么跑」与「记成什么」因此不允许分家。
    """

    with session_scope() as session:
        if definition is None:
            row = get_case(session, case_id)
            if row is None:
                raise ValueError(f"case not found: {case_id}")
            snapshot = snapshot_of(row)
        else:
            # 批次路径：**不回头读用例行**。这一句就是「前半批按旧断言、后半批按新断言」
            # 那条路的封口——只要格子各自读当前行，批次执行期间的一次编辑就会把一批结论
            # 悄悄劈成两个定义下的结果，而汇总与版本号还只有一个。
            snapshot = dict(definition)
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
            # 定义摘要与覆盖都在结论产生的那一刻才写（见 _execute）：执行中的这一行不该挂着
            # 一个还没成立的说法，也不该挂着上一轮的前提。整条落下是 docs/protocol.md 第 7 节的
            # 承诺——按列合并会拼出「这次的 run_id + 上次的覆盖」这种自相矛盾的记录。
            last_definition_digest=None,
            last_definition_overrides=None,
        )

    # 这次执行显式给出的回放覆盖（没给的就是没给）。预算不在其中：它不改变判据。
    overrides = run_overrides(
        from_seq=from_seq,
        preset=preset,
        policy=policy,
        model=model,
        system_prompt=system_prompt,
        budget=budget,
    )
    # 有效定义 = 用例定义 ⊕ 覆盖。**只在这里合并一次**：下面的计划与最终的 last_definition_digest
    # 用的是同一个快照。实际从第 1 步回放却把结论记成第 15 步那一版，根子就是这两处分家。
    effective = effective_snapshot(snapshot, overrides)
    # 给了新的 system prompt 却没有指定模式时，按回归模式执行。
    # 复现模式完全使用录制结果，模型根本不会跑，换 Prompt 也就没有任何意义。
    # 计划在提交前就确定，回放 Run 也因此可以立刻可见，而不是等第一批事件落库。
    plan = resolve_plan(
        effective,
        # 上限只约束这一次执行。整批的上限不在这里：套件层在启动每一格之前把「整批剩余」
        # 作为这一格的额度分下来（见 suites.py），因此这里拿到的额度天然不会越过整批上限。
        budget=budget,
        labels={"case_id": case_id, "case_name": snapshot["name"]},
    )
    prepare_run(plan, run_id, case={"case_id": case_id, "name": snapshot["name"]})
    # 交出去的是**有效定义**：断言求值、定义摘要、覆盖记录都从它来。
    return run_id, plan, effective, overrides


def resolve_plan(
    snapshot: dict[str, Any],
    *,
    budget: ReplayBudget | None = None,
    labels: dict[str, str] | None = None,
) -> ReplayPlan:
    """把「一次用例执行」解析成回放计划。

    执行与整批预估共用这一处：什么时候算回归模式只有这一份判定。两边各写一份的话，
    预估出来的调用量就会与真的跑出来的不是同一件事，而这种偏差在界面上看不出来。

    **传进来的必须是有效定义**（已经合并过覆盖的快照，见 `effective_snapshot`）：覆盖不再
    作为这里的一串参数出现，因为「合并过的快照」只有一个来源，而两串参数可以各传各的。
    这里还会**再归一化一遍**（幂等，见 `normalized`）：计划因此不可能与摘要分家，哪怕调用方
    递进来的是一份原始用例行（把 `from_seq=0` 记成 0 却从第 1 步跑，就是这么来的）。

    副作用策略也从快照里取（覆盖已经合并进去了），没有独立的 policy 参数：多一条并行通道，
    就多一次「计划与摘要各说一套」的机会——上一轮那条错误归属正是这么来的。
    """

    effective = normalized(snapshot)
    # 给了新的 system prompt 却没有指定模式时，按回归模式执行。
    # 复现模式完全使用录制结果，模型根本不会跑，换 Prompt 也就没有任何意义。
    resolved_preset = _preset(effective.get("preset"))
    if effective.get("system_prompt") is not None and resolved_preset is None:
        resolved_preset = ReplayPreset.REGRESS

    return build_plan(
        effective["source_run_id"],
        # `or 1` 留着只是最后一道防线：`normalized` 已经把它定下来了（两者同一读法）。
        from_seq=effective["from_seq"] or 1,
        preset=resolved_preset,
        policy=_policy(effective.get("policy")),
        budget=budget,
        model=effective.get("model"),
        system_prompt=effective.get("system_prompt"),
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
    overrides: dict[str, Any] | None = None,
) -> None:
    try:
        # 只把**真的给了**的额外参数传下去：单条执行路径（一个都没有）原样调用，
        # 因此有些调用方（含测试替身）只按位置接收参数也不会被迫改签名。
        extras: dict[str, Any] = {}
        if on_result is not None:
            extras["on_result"] = on_result
        if condition is not None:
            extras["condition"] = condition
        if shared_ledger is not None:
            extras["shared_ledger"] = shared_ledger
        if overrides:
            extras["overrides"] = overrides
        _execute(plan, run_id, case_id, snapshot, **extras)
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
        _store(
            case_id,
            run_id,
            "error",
            results,
            cause,
            condition,
            definition_digest_of(snapshot),
            overrides,
        )
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
    overrides: dict[str, Any] | None = None,
) -> None:
    # 这次执行用的是**哪一版定义**，与结论一起落库。snapshot 在这里已经是**有效定义**
    # （用例定义 ⊕ 这次显式给出的覆盖，见 _prepare_case_run），因此这一句记的是「按什么判的」
    # 本身，而不是「用例行上写的是什么」。
    definition_digest = definition_digest_of(snapshot)
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
    _store(case_id, run_id, verdict, payload, cause, condition, definition_digest, overrides)
    if on_result is not None:
        on_result(verdict, payload, cause)


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
    definition_digest: str | None = None,
    overrides: dict[str, Any] | None = None,
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
                # 这一版定义的摘要（**有效定义**：用例定义盖上这次显式给出的覆盖）。
                # 它是「这次结论是在哪一版定义下得出的」那句话本身；只记基础定义会让一次
                # 「从第 1 步回放」的结论被记成「第 15 步那一版」，那是错误归属。
                last_definition_digest=definition_digest,
                # 覆盖本身（结构化；空 = 没有覆盖）。只有摘要而没有它，读者会以为摘要不同
                # 一定是「定义被改了」——那是第二句没有证据支撑的话。
                last_definition_overrides=dict(overrides) if overrides else None,
            )
    except Exception as exc:  # noqa: BLE001
        # 带上库名：这一句是「后台线程连到了别的库」时唯一的现场线索（engine 按当前设置
        # 懒建，线程跑过换库点就会漂走）。只有一个 `no such table` 几乎无法定位——issue #18
        # 的 CI 日志里就只有一个表名。
        logger.warning("failed to persist case result: %s（db=%s）", exc, bound_db_path())
