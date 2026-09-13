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
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent_flight_recorder.models import ReplayPreset, new_id, utcnow
from agent_flight_recorder.registry import resolve_agent_spec
from agent_flight_recorder.replay.reasons import InconclusiveCode, InconclusiveReason

from .cases import ResultHook, run_case_blocking
from .db import session_scope
from .storage import (
    aware_utc,
    get_case,
    get_run,
    get_suite,
    list_cases,
    list_suite_items,
    list_suites,
)
from .tables import CaseTable, SuiteItemTable, SuiteTable

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="afr-suite")

#: 一次「全部用例」最多带上多少条，与用例列表端点的上限一致。
ALL_CASES_LIMIT = 500

#: 一个有结论的格子只有这四种取值，它们同时也是用例页的四态。
VERDICTS: tuple[str, ...] = ("passed", "failed", "inconclusive", "error")
#: 还没跑完的两种取值。它们不是结论，因此绝不并进 failed。
OPEN_STATUSES: tuple[str, ...] = ("pending", "running")
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
) -> str:
    """提交一次批量运行，立即返回批次标识；真正的执行全部在线程池里。

    所有格子（用例 × 条件）在返回前就落库为 pending，因此「总数 / 已完成数」从第一秒
    起就是可查的真值，而不是等执行完才知道分母。
    """

    with session_scope() as session:
        cases = _resolve_targets(session, case_ids=list(case_ids or []), all_cases=all_cases)
        requested = _requested_conditions(conditions)
        ordered_case_ids = [case.id for case in cases]
        cells = [
            (case.id, case.name, _cell_condition(session, case, requested_condition))
            for case in cases
            for requested_condition in requested
        ]

    suite_id = new_id()
    queued: list[tuple[str, str, dict[str, Any]]] = []
    with session_scope() as session:
        session.add(
            SuiteTable(
                id=suite_id,
                status="running",
                case_ids=ordered_case_ids,
                conditions=requested,
                created_at=utcnow(),
            )
        )
        for position, (case_id, case_name, condition) in enumerate(cells):
            item_id = new_id()
            queued.append((item_id, case_id, condition))
            session.add(
                SuiteItemTable(
                    id=item_id,
                    suite_id=suite_id,
                    case_id=case_id,
                    case_name=case_name,
                    position=position,
                    condition_key=condition_key(condition),
                    condition=condition,
                    status="pending",
                )
            )

    for item_id, case_id, condition in queued:
        _executor.submit(_run_item, suite_id, item_id, case_id, condition)
    return suite_id


# ------------------------------------------------------------------ 执行


def _run_item(suite_id: str, item_id: str, case_id: str, condition: dict[str, Any]) -> None:
    """跑一个格子。这里吞掉所有异常：单条用例失败不能拖垮整批。"""

    _update_item(item_id, status="running", started_at=utcnow())
    preset = ReplayPreset(condition["preset"]) if condition.get("preset") else None

    def _record(verdict: str, results: list[dict[str, Any]], cause: InconclusiveReason | None) -> None:
        _finish_item(item_id, verdict, results, cause)

    on_result: ResultHook = _record
    try:
        run_case_blocking(
            case_id,
            preset=preset,
            model=condition.get("model"),
            system_prompt=condition.get("system_prompt"),
            on_started=lambda run_id: _update_item(item_id, run_id=run_id),
            on_result=on_result,
            condition=condition,
        )
    except Exception as exc:  # noqa: BLE001 - 格子级失败也要留下可诊断的结论
        logger.warning("suite item failed (suite=%s item=%s): %s", suite_id, item_id, exc)
        message = f"{type(exc).__name__}: {exc}"
        _finish_item(
            item_id,
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
        _close_suite_if_done(suite_id)


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
        if any(item.status not in VERDICTS for item in items):
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
    return _payload(suite, list_suite_items(session, suite_id))


def suite_summaries(session: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    return [
        _summary(suite, list_suite_items(session, suite.id))
        for suite in list_suites(session, limit=limit)
    ]


def _payload(suite: SuiteTable, items: list[SuiteItemTable]) -> dict[str, Any]:
    counts = _tally(items)
    return {
        "id": suite.id,
        "status": _status(items),
        "created_at": aware_utc(suite.created_at),
        "finished_at": aware_utc(suite.finished_at),
        "case_ids": list(suite.case_ids or []),
        "conditions": [dict(condition) for condition in (suite.conditions or [])],
        "total": len(items),
        "completed": _completed(counts),
        # 批次级的四态计数：这是计数，不是一个分数——它不跨条件做任何加权或合并。
        "counts": counts,
        "errors": counts["error"],
        # 按条件分组的呈现。刻意不提供任何跨条件的合计比率：把不同条件下的数字加总
        # 成一个分数，正是 PRD 6.5 明确反对的做法。
        "groups": _groups(suite, items),
    }


def _summary(suite: SuiteTable, items: list[SuiteItemTable]) -> dict[str, Any]:
    counts = _tally(items)
    return {
        "id": suite.id,
        "status": _status(items),
        "created_at": aware_utc(suite.created_at),
        "finished_at": aware_utc(suite.finished_at),
        "conditions": [dict(condition) for condition in (suite.conditions or [])],
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
    unfinished = counts["pending"] + counts["running"]
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

    return "running" if any(item.status not in VERDICTS for item in items) else "finished"


__all__ = [
    "ALL_CASES_LIMIT",
    "OPEN_STATUSES",
    "STATUS_ORDER",
    "VERDICTS",
    "SuiteRequestError",
    "condition_key",
    "condition_label",
    "submit_suite",
    "suite_payload",
    "suite_summaries",
]
