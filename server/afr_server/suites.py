"""批量套件：一次跑一批用例 × 一组条件，并给出按条件分组的汇总。

这一层要回答的问题是「改完到底有没有变好」，因此有三条不能违反的约束（issue #10）：

* **条件随结果记录** —— 每个格子都记住自己属于哪个 Prompt 版本 / 模型。同一条用例
  在不同条件下的结论分别可辨认，不存在「这条用例上次是什么条件下的结论」这种歧义。
* **不合成总分** —— 汇总只给每个条件自己的四态计数与可判断率（分母是该条件自己的
  总数），不做百分制、不做加权分、不把不同条件加总成一个数字（见 PRD 6.5）。
* **inconclusive 不等于 failed** —— 聚合沿用第 3 轮建立的 code + detail，把「拿不到
  结论」与「结论是不通过」分列，不在聚合层又把它们并回去。

批次只是编排，不复制执行逻辑：每个格子走的都是 Cases 模块的同一条链路
（run_case_blocking），因此单条运行与批量运行不会被两套实现慢慢拉偏。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent_flight_recorder.models import ReplayBudget, ReplayPreset, new_id, utcnow
from agent_flight_recorder.registry import resolve_agent_spec
from agent_flight_recorder.replay.budget import (
    BudgetLedger,
    BudgetUsage,
    estimate_replay_budget,
    usage_detail,
)
from agent_flight_recorder.replay.reasons import InconclusiveCode, InconclusiveReason

from .case_versions import (
    case_set_payload,
    case_set_version_id,
    effective_snapshot,
    record_version,
    run_overrides,
    snapshot_of,
)
from .cases import ResultHook, resolve_plan, run_case_blocking
from .db import session_scope
from .storage import (
    aware_utc,
    get_case,
    get_events,
    get_run,
    get_suite,
    list_cases,
    list_suite_items,
    list_suites,
    run_to_record,
)
from .tables import CaseTable, SuiteItemTable, SuiteTable

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="afr-suite")

#: 一次「全部用例」最多带上多少条，与用例列表端点的上限一致。
ALL_CASES_LIMIT = 500

#: 一个有结论的格子只有这四种取值，它们同时也是用例页的四态。
VERDICTS: tuple[str, ...] = ("passed", "failed", "inconclusive", "error")
#: 「未启动」：格子从未被执行过，因此它没有任何结论。它与 pending 不是一回事——
#: pending 是「还没轮到」，not_started 是「不会再轮到，因为整批的预算已经用尽」。
NOT_STARTED: str = "not_started"
#: 「未启动」的原因。批量记账的句子与控制台的说法都用它，跨语言契约测试钉住逐字一致。
NOT_STARTED_REASON: str = "因批次预算用尽"
NOT_STARTED_LABEL: str = f"未启动（{NOT_STARTED_REASON}）"
#: 还没跑完的三种取值。它们不是结论，因此绝不并进 failed，也不并进 undecided。
OPEN_STATUSES: tuple[str, ...] = ("pending", "running", NOT_STARTED)
#: 批次可以就此结束的状态。not_started 是终态：那个格子不会再有结论，等它没有意义。
TERMINAL_STATUSES: tuple[str, ...] = VERDICTS + (NOT_STARTED,)
#: 聚合呈现的固定顺序：先四态，再未完成态。
STATUS_ORDER: tuple[str, ...] = VERDICTS + OPEN_STATUSES


class SuiteRequestError(ValueError):
    """批量请求本身不合法（用户可修的 4xx），与执行期的单条失败区分开。

    单条用例执行失败不能拖垮整批，那些失败落在格子上；这里抛出的只可能是
    「这个批量请求没法成立」。
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# ------------------------------------------------------------------ 条件


def condition_key(condition: dict[str, Any]) -> str:
    """条件的规范化键。分组只认它，界面因此不会把两个条件混成一组。"""

    return f"prompt={condition.get('prompt') or ''}|model={condition.get('model') or ''}"


def condition_label(condition: dict[str, Any]) -> str:
    """条件的可读标签：只写这个条件**实际覆盖了**什么。

    不确定的维度要如实说成「沿用用例自身」，而不是替它起一个名字（例如「默认模型」）：
    没有覆盖时，每个格子用的是各自用例自己的模型，服务端并不能替它们担保某个「默认」。
    """

    prompt = condition.get("prompt")
    model = condition.get("model")
    if not prompt and not model:
        return "沿用用例自身条件"
    return f"{prompt or '沿用用例自身 Prompt'} · {model or '沿用用例自身模型'}"


def _requested_conditions(conditions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """归一化请求里的条件：只保留 prompt / model 两维，并按名字去重。

    去重是有意的：同一批里出现两个完全相同的条件，只会得到两列一模一样、谁也
    不知道该看哪一列的结果。
    """

    requested: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in conditions or [{}]:
        condition = {"prompt": (raw.get("prompt") or None), "model": (raw.get("model") or None)}
        key = condition_key(condition)
        if key in seen:
            continue
        seen.add(key)
        requested.append(condition)
    if not requested:
        requested.append({"prompt": None, "model": None})
    return requested


def _cell_condition(session: Any, case: CaseTable, requested: dict[str, Any]) -> dict[str, Any]:
    """把请求里的条件落到某条用例上：解析出 Prompt 正文，并定下执行模式。"""

    prompt = requested.get("prompt")
    model = requested.get("model")
    return {
        "prompt": prompt,
        "model": model,
        # 正文一并落库：只记名字的话，预设以后被改了，历史格子就说不清当时用的是哪一版。
        "system_prompt": _preset_prompt(session, case, prompt) if prompt else None,
        # 有覆盖就必须真的让模型跑（回归模式）。复现模式完全使用录制结果，换 Prompt
        # 或换模型不会产生任何影响——那样的格子会给出一个看起来正常、其实什么都没
        # 验证的结论，这是「改完到底有没有变好」最容易骗到人的地方。
        "preset": ReplayPreset.REGRESS.value if (prompt or model) else None,
    }


def _condition_run_args(condition: dict[str, Any]) -> dict[str, Any]:
    """一个条件里**参与执行前提**的那三项，按执行入口认的形态给出（preset 是枚举）。

    `prompt` 只是条件的标签（版本名），正文在 `system_prompt` 里，因此标签不进前提。
    """

    return {
        "preset": ReplayPreset(condition["preset"]) if condition.get("preset") else None,
        "model": condition.get("model"),
        "system_prompt": condition.get("system_prompt"),
    }


def _condition_premise(condition: dict[str, Any]) -> dict[str, Any]:
    """同一份前提的**记录形态**（JSON 友好）：摘要与覆盖列都从它来。

    与 `_condition_run_args` 是同一处映射的两种形态：执行要枚举、记录要字符串，
    因此两边永远说的是同一件事（case_versions.run_overrides 同时负责规范化）。
    """

    return run_overrides(**_condition_run_args(condition))


def _preset_prompt(session: Any, case: CaseTable, name: str) -> str:
    """把 Prompt 版本名解析成正文。解析不了就当场报错，而不是跑出一个假结论。"""

    source = get_run(session, case.source_run_id)
    spec = resolve_agent_spec(source.agent_name) if source else None
    presets = dict(spec.prompt_presets) if spec else {}
    if name not in presets:
        available = "、".join(sorted(presets)) or "（该 Agent 没有声明任何 Prompt 版本）"
        agent = source.agent_name if source else "未知 Agent"
        raise SuiteRequestError(f"Agent {agent} 没有名为 {name!r} 的 Prompt 版本，可选：{available}")
    return presets[name]


# ------------------------------------------------------------------ 提交


def _resolve_targets(session: Any, *, case_ids: list[str], all_cases: bool) -> list[CaseTable]:
    if all_cases:
        rows = list_cases(session, limit=ALL_CASES_LIMIT)
        if not rows:
            raise SuiteRequestError("还没有任何用例可以跑")
        return rows
    if not case_ids:
        raise SuiteRequestError("必须给出 case_ids，或显式声明 all_cases")

    rows: list[CaseTable] = []
    seen: set[str] = set()
    for case_id in case_ids:
        if case_id in seen:
            continue
        seen.add(case_id)
        row = get_case(session, case_id)
        if row is None:
            raise SuiteRequestError(f"case not found: {case_id}", status_code=404)
        rows.append(row)
    return rows


def submit_suite(
    *,
    case_ids: list[str] | None = None,
    all_cases: bool = False,
    conditions: list[dict[str, Any]] | None = None,
    budget: ReplayBudget | None = None,
) -> str:
    """提交一次批量运行，立即返回批次标识；真正的执行全部在线程池里。

    所有格子（用例 × 条件）在返回前就落库为 pending，因此「总数 / 已完成数」从第一秒
    起就是可查的真值，而不是等执行完才知道分母。

    budget 是**整批**的硬上限（两个维度都与单次回放同一套语义）。不声明 = 与没有这套
    能力时逐字一致：调度、状态取值、汇总字段一个都不变。
    """

    declared = budget if budget is not None and budget.is_set else None
    #: 提交这一刻冻结的定义：键是 case_id，值是执行要用的整份快照。
    frozen: dict[str, dict[str, Any]] = {}

    with session_scope() as session:
        cases = _resolve_targets(session, case_ids=list(case_ids or []), all_cases=all_cases)
        requested = _requested_conditions(conditions)
        ordered_case_ids: list[str] = []
        cells: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        for case in cases:
            ordered_case_ids.append(case.id)
            # 每个格子拿到的是**这一份**快照。执行期不再读用例行：批次提交之后的任何一次
            # 编辑都不该改变这一批已经定下的判据（见 case_versions.py 与 cases._prepare_case_run）。
            frozen[case.id] = snapshot_of(case)
            for requested_condition in requested:
                cells.append(
                    (
                        case.id,
                        case.name,
                        frozen[case.id],
                        _cell_condition(session, case, requested_condition),
                    )
                )
        definitions = [frozen[case_id] for case_id in ordered_case_ids]

    suite_id = new_id()
    # 版本标识是纯计算（内容决定），因此可以和套件、格子放进同一个事务。
    case_set_version = case_set_version_id(definitions)
    queued: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    with session_scope() as session:
        # 版本与套件同一个事务：新套件不可能出现「挂着版本标识、却反查不到定义」的状态。
        # 标识即主键，因此重复提交同一份定义只会有同一行——「两个套件是不是同一版用例」
        # 由标识本身回答，不需要事后比对。
        record_version(session, definitions)
        session.add(
            SuiteTable(
                id=suite_id,
                status="running",
                case_ids=ordered_case_ids,
                conditions=requested,
                # 声明的整批上限；没声明时是 None。记账对象与上限声明分开存：没有声明过
                # 上限的批次不应该凭空多出一个「上限：无」的账目。
                budget=declared.model_dump(mode="json") if declared else None,
                case_set_version=case_set_version,
                created_at=utcnow(),
            )
        )
        for position, (case_id, case_name, definition, condition) in enumerate(cells):
            item_id = new_id()
            queued.append((item_id, case_id, condition, definition))
            session.add(
                SuiteItemTable(
                    id=item_id,
                    suite_id=suite_id,
                    case_id=case_id,
                    case_name=case_name,
                    position=position,
                    condition_key=condition_key(condition),
                    condition=condition,
                    # 格子自己带着版本：它才是被执行的单位，「我跑的是哪一版」不必回头读套件。
                    case_set_version=case_set_version,
                    status="pending",
                )
            )

    if declared is None:
        for item_id, case_id, condition, definition in queued:
            _executor.submit(_run_item, suite_id, item_id, case_id, condition, definition)
    else:
        # 声明了整批上限就交给一个串行调度：见 _run_batch 里为什么必须逐格来。
        _executor.submit(_run_batch, suite_id, list(queued), declared)
    return suite_id


# ------------------------------------------------------------------ 预估（只读）


def estimate_suite(
    *,
    case_ids: list[str] | None = None,
    all_cases: bool = False,
    conditions: list[dict[str, Any]] | None = None,
    budget: ReplayBudget | None = None,
) -> dict[str, Any]:
    """预估整批要花多少：把每一格的预估聚合起来。

    纯计算：不调用任何模型，也不创建 Run。用的是与真的执行同一套计划解析与同一份录制
    （estimate_replay_budget 只读父 Run），因此「预估」与「实跑」不会是两件事。

    缺数据时整批就是「无法预估」：只要有一格给不出成本，整批的成本就是 None，并给出
    原因。**绝不拿部分数据凑一个确定数字**——那是「不得把未知当 0」在整批这一层的写法。
    """

    declared = budget if budget is not None and budget.is_set else None

    with session_scope() as session:
        cases = _resolve_targets(session, case_ids=list(case_ids or []), all_cases=all_cases)
        requested = _requested_conditions(conditions)
        # 计划解析与执行走同一处（cases.resolve_plan），因此「什么时候算回归模式」只有一份。
        plans: list[tuple[str, dict[str, Any], Any]] = [
            (
                case.name,
                condition,
                resolve_plan(
                    # 与真跑一次时同一处合并（cases.effective_snapshot）：预估与实际执行
                    # 对「这一格按什么跑」必须是同一个答案。
                    effective_snapshot(snapshot_of(case), _condition_premise(condition)),
                ),
            )
            for case in cases
            for condition in (
                _cell_condition(session, case, requested_condition)
                for requested_condition in requested
            )
        ]

        # 同一份父 Run 的证据只读一次：一批用例常常来自同一次失败，重复读没有意义。
        evidence: dict[str, tuple[Any, list[Any]]] = {}
        estimates: list[tuple[str, dict[str, Any], Any]] = []
        for name, condition, plan in plans:
            if plan.parent_run_id not in evidence:
                row = get_run(session, plan.parent_run_id)
                if row is None:
                    raise SuiteRequestError(
                        f"用例的源运行不存在：{plan.parent_run_id}", status_code=404
                    )
                evidence[plan.parent_run_id] = (
                    run_to_record(row),
                    get_events(session, plan.parent_run_id),
                )
            parent_run, parent_events = evidence[plan.parent_run_id]
            estimates.append(
                (name, condition, estimate_replay_budget(plan, parent_run, parent_events))
            )

    cells = len(estimates)
    calls = sum(estimate.model_calls for _, _, estimate in estimates)
    # 只要有格子给不出成本，整批就是「无法预估」：把已知的那些加起来报一个确定数字，
    # 等于把未知当成 0（也就等于说那些调用没花钱）。
    unknown = [
        f"{name}（{condition_label(condition)}）：{estimate.detail}"
        for name, condition, estimate in estimates
        if estimate.cost_usd is None
    ]
    cost: float | None = (
        None
        if unknown
        else round(sum(float(estimate.cost_usd or 0.0) for _, _, estimate in estimates), 6)
    )

    notes: list[str] = []
    if declared is not None:
        if declared.max_model_calls is not None and calls > declared.max_model_calls:
            calls = declared.max_model_calls
            notes.append(
                f"声明的整批上限是 {declared.max_model_calls} 次真实模型调用，因此预计最多跑这么多。"
            )
        if declared.max_cost_usd is not None:
            if cost is None:
                # 成本未知时这一维**根本不会触发**（BudgetLedger.exceeded_by 在 cost 为
                # None 时跳过成本判定），因此这里不能写成「不会超过它」：那不是「守住了
                # 上限」，而是「这一维没有生效」。缺数据就让缺数据自己说话，并说清这一次
                # 声明的实际约束落在哪一维（没有调用次数上限时就是没有约束）。
                if declared.max_model_calls is not None:
                    bound = (
                        f"这一次的实际约束落在调用次数上限上（{declared.max_model_calls} 次），"
                        "成本这一维不参与判定。"
                    )
                else:
                    bound = "这一次声明没有产生实际约束。"
                notes.append(
                    f"整批成本上限声明为 ${declared.max_cost_usd}，但这一维无法判定："
                    "有格子的成本按本地价格表算不出来，未知不会被当成 0，因此它不会在成本到点时停下；"
                    f"{bound}"
                )
            elif cost > declared.max_cost_usd:
                # 不把预估数字改写成上限值：那会读成「预计就花这么多」。实际语义是
                # 「到点后不再发起新调用，已经发出的那次允许跑完」——成本可能比上限多出
                # 最后一次调用（见 docs/replay-semantics.md 10.3）。调用次数那一维可以裁，
                # 因为次数在发出之前就知道；成本这一维不行。
                notes.append(
                    f"整批成本上限是 ${declared.max_cost_usd}，低于上面的估算："
                    "达到上限后会在步边界停止、不再发起新的调用；已经发出的那次调用允许完成并如实记账，"
                    "因此实际成本可能比上限多出最后一次调用，后面的格子不会启动。"
                )

    if calls == 0 and not unknown:
        detail = (
            f"整批 {cells} 个格子都不会产生真实模型调用（全部使用录制结果），"
            "预计 0 次调用、0 成本。"
        )
    elif unknown:
        detail = (
            f"整批 {cells} 个格子的合计预估：预计 {calls} 次真实模型调用；"
            f"成本无法预估（{len(unknown)} 个格子给不出成本）。例如 {unknown[0]}"
        )
    else:
        detail = (
            f"整批 {cells} 个格子的合计预估：预计 {calls} 次真实模型调用，"
            f"估算成本约 ${cost:.6f}（估算）。价格表是本地快照，回归模式下新上下文与"
            "父 Run 也不完全相同，因此这只是估算。"
        )

    return {
        "cells": cells,
        "model_calls": calls,
        "cost_usd": cost,
        "cost_is_estimate": True,
        "detail": detail + "".join(notes),
    }


# ------------------------------------------------------------------ 执行


def _run_item(
    suite_id: str,
    item_id: str,
    case_id: str,
    condition: dict[str, Any],
    definition: dict[str, Any],
    *,
    budget: ReplayBudget | None = None,
    shared_ledger: BudgetLedger | None = None,
    publish_batch: Callable[[], None] | None = None,
) -> None:
    """跑一个格子。这里吞掉所有异常：单条用例失败不能拖垮整批。

    definition 是提交那一刻冻结的用例定义：这一格按它跑，不读当前用例行（见 submit_suite）。

    budget / shared_ledger 由整批调度给（没有整批上限时两个都不传）：
    这一格的额度是「整批剩余」，同一次真实调用也会记进整批的账本。

    publish_batch 也是整批调度给的。它有两个后果，而且是一个意思——「这一格由一个会自己
    记账的调度管着」：

    * 格子的结论落库**之前**先把整批的账写下去。否则最后那一格的结论一落库，批次就已经
      算「结束」了（状态由格子的状态推导），而账还停在上一格那一刻——控制台会读到一个
      「已结束」但数字不对的批次；
    * 这一格跑完不自己给批次收尾：整批还有格子要跑（或要标未启动）。
    """

    _update_item(item_id, status="running", started_at=utcnow())

    def _finish(
        verdict: str, results: list[dict[str, Any]], cause: InconclusiveReason | None
    ) -> None:
        if publish_batch is not None:
            publish_batch()
        _finish_item(item_id, verdict, results, cause)

    def _record(verdict: str, results: list[dict[str, Any]], cause: InconclusiveReason | None) -> None:
        _finish(verdict, results, cause)

    on_result: ResultHook = _record
    # 没有整批上限时一个多余关键字都不传：这条路径上的调用方（含测试替身）不该因为
    # 批量多了一个能力就被迫改签名。
    batch_args: dict[str, Any] = (
        {"budget": budget, "shared_ledger": shared_ledger}
        if budget is not None or shared_ledger is not None
        else {}
    )
    try:
        run_case_blocking(
            case_id,
            definition=definition,
            # 执行参数与记录前提来自同一处映射：格子的前提不会随调用点而变。
            **_condition_run_args(condition),
            on_started=lambda run_id: _update_item(item_id, run_id=run_id),
            on_result=on_result,
            condition=condition,
            **batch_args,
        )
    except Exception as exc:  # noqa: BLE001 - 格子级失败也要留下可诊断的结论
        logger.warning("suite item failed (suite=%s item=%s): %s", suite_id, item_id, exc)
        message = f"{type(exc).__name__}: {exc}"
        _finish(
            "error",
            [
                {
                    "spec": {"type": "suite_item", "value": None, "note": ""},
                    "passed": False,
                    "detail": message,
                }
            ],
            InconclusiveReason.from_code(InconclusiveCode.UNKNOWN.value, message),
        )
    finally:
        if publish_batch is None:
            _close_suite_if_done(suite_id)


def _run_batch(
    suite_id: str,
    queued: list[tuple[str, str, dict[str, Any], dict[str, Any]]],
    budget: ReplayBudget,
) -> None:
    """整批预算下的调度：**按顺序逐格**执行，启动前先看整批的账。

    为什么必须逐格，而不是像没有上限时那样并发：每一格拿到的额度是「整批剩余」。
    两个格子同时启动时，它们看到的「剩余」都会是启动前的那一份，两格加起来就已经越过
    整批上限了——那时「整批不超过上限」只能靠运气成立。逐格之后，上一格的真实用量在
    **下一格启动之前**已经进了整批账本，于是「整批不超过上限」是一条可保证的结论。

    到点之后不再启动任何新格子：剩下的格子标成「未启动（因批次预算用尽）」，它们从未
    被执行过，因此不是 failed、不是 inconclusive、也不是 error，也不该留在 pending 里
    让界面一直等。已经启动的那一格允许跑完（不做预测性中断），它花掉的每一分都如实记账。
    """

    ledger = BudgetLedger(budget)

    def publish() -> None:
        _publish_suite_budget(suite_id, ledger)

    try:
        for item_id, case_id, condition, definition in queued:
            try:
                stopped_by = ledger.exceeded_by()
                if stopped_by is None:
                    _run_item(
                        suite_id,
                        item_id,
                        case_id,
                        condition,
                        definition,
                        budget=ledger.remaining(),
                        shared_ledger=ledger,
                        publish_batch=publish,
                    )
                else:
                    # 触顶这一维只在这里记下：它是「整批为什么停下」的答案。
                    # 顺序要紧：先把这一笔写进账，再让格子变成终态。反过来的话，最后
                    # 一格一变终态，批次就已经算「结束」了，而账还差这一笔。
                    ledger.stopped_by = stopped_by
                    publish()
                    _mark_not_started(item_id)
            except Exception as exc:  # noqa: BLE001 - 一格出问题不能带走整批的其余格子
                logger.warning("suite batch step failed (suite=%s item=%s): %s", suite_id, item_id, exc)
            publish()
    finally:
        publish()
        _close_suite_if_done(suite_id)


def _mark_not_started(item_id: str) -> None:
    """把一个从未启动的格子如实标出来。

    刻意不写 cause：成因只属于**跑过了**的两类（inconclusive 与 error）。一个从未执行
    过的格子没有任何成因可言，替它编一个等于对一个没跑过的格子下结论。原因由状态本身
    与整批的记账一起表达（见 docs/protocol.md 第 7 节）。
    """

    _update_item(item_id, status=NOT_STARTED, ended_at=utcnow())


def _publish_suite_budget(suite_id: str, ledger: BudgetLedger) -> None:
    """把整批的账目写回批次行。

    整批的账目是它自己的真值：不声明上限的批次没有它（那时这里根本不会被调用）。
    """

    usage = ledger.usage()
    with session_scope() as session:
        suite = get_suite(session, suite_id)
        if suite is None:
            return
        suite.budget_usage = usage.model_dump(mode="json")
        session.add(suite)


def _update_item(item_id: str, **fields: Any) -> None:
    with session_scope() as session:
        item = session.get(SuiteItemTable, item_id)
        if item is None:
            return
        for name, value in fields.items():
            setattr(item, name, value)
        session.add(item)


def _finish_item(
    item_id: str,
    verdict: str,
    results: list[dict[str, Any]],
    cause: InconclusiveReason | None,
) -> None:
    _update_item(
        item_id,
        status=verdict,
        results=results,
        # 结论与成因一起落库：只有结论没有成因的格子，用户无从知道该去看录制质量
        # 还是副作用策略。
        cause=cause.model_dump(mode="json") if cause else None,
        ended_at=utcnow(),
    )


def _close_suite_if_done(suite_id: str) -> None:
    with session_scope() as session:
        items = list_suite_items(session, suite_id)
        # 「未启动」也是终态：那个格子不会再有结论，等它没有意义（否则整批永远停在
        # 「进行中」，界面也会一直轮询一个其实已经停下的批次）。
        if any(item.status not in TERMINAL_STATUSES for item in items):
            return
        suite = get_suite(session, suite_id)
        if suite is None or suite.status == "finished":
            return
        suite.status = "finished"
        suite.finished_at = utcnow()
        session.add(suite)


# ------------------------------------------------------------------ 聚合


def suite_payload(session: Any, suite_id: str) -> dict[str, Any] | None:
    suite = get_suite(session, suite_id)
    if suite is None:
        return None
    return _payload(session, suite, list_suite_items(session, suite_id))


def suite_summaries(session: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    return [
        _summary(suite, list_suite_items(session, suite.id))
        for suite in list_suites(session, limit=limit)
    ]


def _payload(session: Any, suite: SuiteTable, items: list[SuiteItemTable]) -> dict[str, Any]:
    counts = _tally(items)
    return {
        "id": suite.id,
        "status": _status(items),
        "created_at": aware_utc(suite.created_at),
        "finished_at": aware_utc(suite.finished_at),
        "case_ids": list(suite.case_ids or []),
        "conditions": [dict(condition) for condition in (suite.conditions or [])],
        # 这批跑的是哪一版用例集：提交那一刻固化的定义标识（可反查，见 case_versions.py）。
        # 历史批次没有它，这里如实是 None——「无版本记录」是真实状态，不是一个缺失的默认值。
        # drift 只说「用例自本批之后被改过」，不改变本批任何一条结论的归属。
        "case_set": case_set_payload(session, suite),
        "total": len(items),
        "completed": _completed(counts),
        # 批次级的四态计数：这是计数，不是一个分数——它不跨条件做任何加权或合并。
        "counts": counts,
        "errors": counts["error"],
        # 整批的预算记账。没声明过整批上限时是 None：不设上限不等于「上限为 0」，
        # 也不该凭空多出一个「上限：无」的账目。
        "budget": suite_budget_payload(suite, items),
        # 按条件分组的呈现。刻意不提供任何跨条件的合计比率：把不同条件下的数字加总
        # 成一个分数，正是 PRD 6.5 明确反对的做法。
        "groups": _groups(suite, items),
    }


def suite_budget_payload(
    suite: SuiteTable, items: list[SuiteItemTable]
) -> dict[str, Any] | None:
    """整批的预算记账：已用 / 上限 / 是否触顶 / 有多少格子因此没跑。

    字段名与单次回放的记账完全一样（同一份 BudgetUsage），因此「已用」「未知」这些说法
    在两个层级上是同一个意思，不存在第二套叫法。多出来的只有 not_started 这一个计数：
    它是批次才有的概念——单次回放里没有「格子」。
    """

    if not suite.budget:
        return None
    declared = dict(suite.budget)
    state = dict(suite.budget_usage or {})
    usage = BudgetUsage(
        max_cost_usd=state.get("max_cost_usd", declared.get("max_cost_usd")),
        max_model_calls=state.get("max_model_calls", declared.get("max_model_calls")),
        model_calls_used=int(state.get("model_calls_used") or 0),
        cost_used_usd=state.get("cost_used_usd"),
        cost_unknown=bool(state.get("cost_unknown")),
        exceeded=bool(state.get("exceeded")),
        stopped_by=state.get("stopped_by"),
    )
    not_started = sum(1 for item in items if item.status == NOT_STARTED)
    payload = usage.model_dump(mode="json")
    payload["detail"] = batch_usage_detail(usage, not_started)
    payload["not_started"] = not_started
    return payload


def batch_usage_detail(usage: BudgetUsage, not_started: int) -> str:
    """整批记账的说明：单次回放那一句 + 「多少格子没跑、为什么」。

    触顶时这里不会出现「跑完了」「全部通过」「拿不到结论」这类措辞：没跑的格子既不是
    通过，也不是「跑过了但拿不到结论」——它们只是没跑。
    """

    detail = usage_detail(usage)
    if not_started:
        return (
            detail
            + f"整批有 {not_started} 个格子{NOT_STARTED_LABEL}，"
            "它们从未执行，因此没有任何结论，也不计入未通过或无法判断。"
        )
    return detail


def _summary(suite: SuiteTable, items: list[SuiteItemTable]) -> dict[str, Any]:
    counts = _tally(items)
    return {
        "id": suite.id,
        "status": _status(items),
        "created_at": aware_utc(suite.created_at),
        "finished_at": aware_utc(suite.finished_at),
        "conditions": [dict(condition) for condition in (suite.conditions or [])],
        # 批次列表也要能一眼看出「这批是哪一版用例」：趋势与对比都建立在这个归属上。
        "case_set_version": getattr(suite, "case_set_version", None),
        "total": len(items),
        "completed": _completed(counts),
        "counts": counts,
        "errors": counts["error"],
    }


def _groups(suite: SuiteTable, items: list[SuiteItemTable]) -> list[dict[str, Any]]:
    """按条件分组。组的先后就是用户提交时的条件顺序，不按好坏重排。"""

    order: list[str] = []
    for condition in suite.conditions or []:
        key = condition_key(condition)
        if key not in order:
            order.append(key)

    buckets: OrderedDict[str, list[SuiteItemTable]] = OrderedDict((key, []) for key in order)
    for item in items:
        buckets.setdefault(item.condition_key, []).append(item)
    return [_group_payload(key, bucket) for key, bucket in buckets.items()]


def _group_payload(key: str, items: list[SuiteItemTable]) -> dict[str, Any]:
    counts = _tally(items)
    total = len(items)
    determinable = counts["passed"] + counts["failed"]
    # 「无法判断」只包括**跑过了、但拿不到可信结论**的格子（inconclusive 与 error）。
    # 尚未跑完的格子必须另外计数：把「还没跑」写成「拿不到结论」，等于对一个从未执行过的
    # 格子下了「跑过了、但拿不到」的实质性断言——那句话没有证据支撑。这与本项目的立身之本
    # （结论只说证据支持的事）冲突，也与第 3 轮把 inconclusive 与 failed 分开的立场同源。
    undecided = counts["inconclusive"] + counts["error"]
    # 「未启动」归在 unfinished 这一侧：它从未执行过，因此既不是「有结论」也不是
    # 「跑过了但拿不到结论」。三个桶依然互斥且穷尽：
    # total == determinable + undecided + unfinished。
    unfinished = counts["pending"] + counts["running"] + counts[NOT_STARTED]
    condition = dict(items[0].condition or {}) if items else {}
    return {
        "condition_key": key,
        "condition": condition,
        "label": condition_label(condition),
        "total": total,
        "completed": _completed(counts),
        "counts": counts,
        # 三个桶互斥且穷尽：total == determinable + undecided + unfinished。
        "determinable": determinable,
        "undecided": undecided,
        # 「还没跑」不是结论，因此单独给一个字段。界面在未跑完时只能陈述三个桶的计数，
        # 不能说出「N 条中 M 条拿不到结论」这种只有整批跑完才成立的句子。
        "unfinished": unfinished,
        # 「未启动（因批次预算用尽）」是 unfinished 里的一个子集，单独给出：它与
        # 「还没轮到」是两回事，界面要能一眼看出哪些格子没跑、为什么没跑。
        "not_started": counts[NOT_STARTED],
        # 可判断率的样本量就是上面这个 total（issue #10 验收 4 明确要求分母是总数），
        # 两者永远一起出现；这里也刻意不给出任何跨条件的合计比率。
        "determinable_rate": (determinable / total) if total else None,
        "errors": counts["error"],
        "items": [_item_payload(item) for item in items],
    }


def _item_payload(item: SuiteItemTable) -> dict[str, Any]:
    return {
        "id": item.id,
        "case_id": item.case_id,
        "case_name": item.case_name,
        # 每个格子都带着自己归属的版本：格子是被执行的单位，它的归属不该靠反查套件才知道。
        "case_set_version": getattr(item, "case_set_version", None),
        "condition_key": item.condition_key,
        "condition": dict(item.condition or {}),
        "status": item.status,
        "run_id": item.run_id,
        "results": list(item.results or []),
        # 成因随格子一起给出：inconclusive 与 error 都要能看到每一条的具体原因。
        "cause": item.cause,
        "started_at": aware_utc(item.started_at),
        "ended_at": aware_utc(item.ended_at),
    }


def _tally(items: list[SuiteItemTable]) -> dict[str, int]:
    counts = {status: 0 for status in STATUS_ORDER}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def _completed(counts: dict[str, int]) -> int:
    return sum(counts.get(status, 0) for status in VERDICTS)


def _status(items: list[SuiteItemTable]) -> str:
    """批次生命周期以格子的实际状态推导，而不是信落库的那一列。

    落库值只是缓存：进程在批量中途重启，格子的状态说没跑完，批次就还是 running，
    而不是永远卡在一个其实已经停下的状态上。
    """

    return "running" if any(item.status not in TERMINAL_STATUSES for item in items) else "finished"


__all__ = [
    "ALL_CASES_LIMIT",
    "NOT_STARTED",
    "NOT_STARTED_LABEL",
    "NOT_STARTED_REASON",
    "OPEN_STATUSES",
    "STATUS_ORDER",
    "TERMINAL_STATUSES",
    "VERDICTS",
    "SuiteRequestError",
    "batch_usage_detail",
    "condition_key",
    "condition_label",
    "estimate_suite",
    "submit_suite",
    "suite_budget_payload",
    "suite_payload",
    "suite_summaries",
]
