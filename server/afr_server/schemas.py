"""API 请求与响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_flight_recorder.models import (
    EffectPolicy,
    Event,
    ReplayBudget,
    ReplayPreset,
    RunRecord,
    RunSummary,
)
from agent_flight_recorder.replay.reasons import InconclusiveReason
from pydantic import BaseModel, Field

from .assertions import AssertionResult, AssertionSpec
from .diff import RunDiff
from .storage import aware_utc
from .tables import CaseTable


class RunListItem(BaseModel):
    run: RunRecord
    summary: RunSummary
    event_count: int
    is_replay: bool


class RunListResponse(BaseModel):
    runs: list[RunListItem]
    total: int


class RunDetailResponse(BaseModel):
    run: RunRecord
    summary: RunSummary
    event_count: int
    parent: RunRecord | None = None
    children: list[RunRecord] = Field(default_factory=list)
    replay: dict[str, Any] | None = None
    case: dict[str, Any] | None = None
    agent_registered: bool = False


class TimelineResponse(BaseModel):
    run: RunRecord
    summary: RunSummary
    events: list[Event]


class ReplayRequest(BaseModel):
    from_seq: int = Field(ge=1)
    preset: ReplayPreset | None = None
    policy: EffectPolicy | None = None
    #: 硬上限（最大成本 / 最大模型调用次数）。两个都不给就是不设上限。
    budget: ReplayBudget | None = None
    model: str | None = None
    system_prompt: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class ReplayEstimateResponse(BaseModel):
    """一次回放的预估。cost_usd 为 null 就是「无法预估」，不是 0。"""

    parent_run_id: str
    from_seq: int
    model_calls: int = 0
    cost_usd: float | None = None
    cost_is_estimate: bool = True
    detail: str = ""


class ReplayResponse(BaseModel):
    run_id: str
    parent_run_id: str
    from_seq: int
    plan_summary: str
    status: str = "running"


class CaseCreateRequest(BaseModel):
    name: str
    description: str = ""
    source_run_id: str
    from_seq: int | None = None
    to_seq: int | None = None
    assertions: list[AssertionSpec] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    preset: ReplayPreset | None = None
    policy: EffectPolicy | None = None
    model: str | None = None
    system_prompt: str | None = None


class CaseItem(BaseModel):
    id: str
    name: str
    description: str
    source_run_id: str
    from_seq: int | None = None
    to_seq: int | None = None
    assertions: list[AssertionSpec] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    preset: ReplayPreset | None = None
    policy: EffectPolicy | None = None
    model: str | None = None
    system_prompt: str | None = None
    last_status: str | None = None
    last_run_id: str | None = None
    last_run_at: datetime | None = None
    last_results: list[AssertionResult] = Field(default_factory=list)
    #: 最近一次结论的成因（结构化的 ``{code, detail}``）；结论为 passed / failed 时为 None，
    #: 也就是 inconclusive 与 error 都必须带上它（见 docs/protocol.md）。
    last_cause: InconclusiveReason | None = None
    #: 最近一次执行用的条件（Prompt 版本 / 模型）；单条运行不携带条件时为 None。
    last_condition: dict[str, Any] | None = None
    created_at: datetime | None = None
    source_run: RunRecord | None = None


class CaseListResponse(BaseModel):
    cases: list[CaseItem]


class CaseRunRequest(BaseModel):
    from_seq: int | None = None
    preset: ReplayPreset | None = None
    #: 这一次执行的硬上限。不给就是不设上限（行为与之前一致）。
    budget: ReplayBudget | None = None
    model: str | None = None
    system_prompt: str | None = None


class CaseRunResponse(BaseModel):
    case_id: str
    run_id: str
    status: str = "running"


class SuiteCondition(BaseModel):
    """矩阵的一列：Prompt 版本与模型。None 表示沿用用例自身的设定。"""

    prompt: str | None = None
    model: str | None = None


class SuiteSubmitRequest(BaseModel):
    case_ids: list[str] = Field(default_factory=list)
    #: 一键全选。与 case_ids 互斥，二者都不给则请求不成立。
    all_cases: bool = False
    #: 条件列表；不给就是「沿用用例自身条件」这一列。
    conditions: list[SuiteCondition] = Field(default_factory=lambda: [SuiteCondition()])


class SuiteSubmitResponse(BaseModel):
    suite_id: str
    status: str = "running"
    total: int
    conditions: list[SuiteCondition] = Field(default_factory=list)


class SuiteItem(BaseModel):
    """套件里的一格：某条用例在某个条件下的结论。"""

    id: str
    case_id: str
    case_name: str
    condition_key: str
    condition: dict[str, Any] = Field(default_factory=dict)
    status: str
    run_id: str | None = None
    results: list[AssertionResult] = Field(default_factory=list)
    cause: InconclusiveReason | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class SuiteConditionGroup(BaseModel):
    """一个条件的汇总。

    只有四态计数与该条件自己的可判断率：这里没有任何跨条件的合计分数，分母就是
    该条件自己的 total。

    三个桶互斥且穷尽：total == determinable + undecided + unfinished。
    undecided 只包括**跑过了、但拿不到可信结论**的格子（inconclusive + error）；
    unfinished 是**还没跑完**的格子（pending + running），它没有任何结论，因此不能
    被算进 undecided——把「还没跑」写成「拿不到结论」是对从未执行过的格子下断言。
    """

    condition_key: str
    condition: dict[str, Any] = Field(default_factory=dict)
    label: str
    total: int
    completed: int
    counts: dict[str, int] = Field(default_factory=dict)
    determinable: int
    undecided: int
    unfinished: int = 0
    determinable_rate: float | None = None
    errors: int
    items: list[SuiteItem] = Field(default_factory=list)


class SuiteDetailResponse(BaseModel):
    id: str
    status: str
    created_at: datetime | None = None
    finished_at: datetime | None = None
    case_ids: list[str] = Field(default_factory=list)
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    total: int
    completed: int
    counts: dict[str, int] = Field(default_factory=dict)
    errors: int
    groups: list[SuiteConditionGroup] = Field(default_factory=list)


class SuiteSummary(BaseModel):
    id: str
    status: str
    created_at: datetime | None = None
    finished_at: datetime | None = None
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    total: int
    completed: int
    counts: dict[str, int] = Field(default_factory=dict)
    errors: int


class SuiteListResponse(BaseModel):
    suites: list[SuiteSummary] = Field(default_factory=list)


class AgentInfo(BaseModel):
    name: str
    description: str = ""
    version: str = ""
    default_model: str | None = None
    default_system_prompt: str | None = None
    prompt_presets: dict[str, str] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)
    can_replay: bool = True
    can_seed: bool = False


def case_to_item(row: CaseTable, source_run: RunRecord | None = None) -> CaseItem:
    policy = row.effect_policy or {}
    return CaseItem(
        id=row.id,
        name=row.name,
        description=row.description,
        source_run_id=row.source_run_id,
        from_seq=row.from_seq,
        to_seq=row.to_seq,
        assertions=[AssertionSpec.model_validate(a) for a in (row.assertions or [])],
        labels=dict(row.labels or {}),
        preset=_preset(policy.get("preset")),
        policy=EffectPolicy.model_validate(policy["policy"]) if policy.get("policy") else None,
        model=policy.get("model"),
        system_prompt=policy.get("system_prompt"),
        last_status=row.last_status,
        last_run_id=row.last_run_id,
        # 时间戳在库里是 naive UTC，必须在这里补回时区：不带 Z 的 ISO 串会被
        # 前端按本地时间解析，刚跑完的用例会显示成 8 小时前（东八区）。
        last_run_at=aware_utc(row.last_run_at),
        last_results=[AssertionResult.model_validate(item) for item in (row.last_results or [])],
        # 历史行没有这个字段（或写着旧的自由文本）时走兼容解析，绝不因此报错。
        last_cause=(
            InconclusiveReason.from_dict(row.last_cause) if getattr(row, "last_cause", None) else None
        ),
        last_condition=(
            dict(row.last_condition) if getattr(row, "last_condition", None) else None
        ),
        created_at=aware_utc(row.created_at),
        source_run=source_run,
    )


def _preset(value: Any) -> ReplayPreset | None:
    if value is None:
        return None
    try:
        return ReplayPreset(str(value))
    except ValueError:
        return None


def replay_meta_to_dict(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 ``metadata.afr_replay`` 归一化成前端契约。

    兼容只做一次，且做在服务端：历史 Run 的 ``reason`` 是自由字符串（甚至有
    ``replay_recording_loss`` 这种已经消失的旧名），归一化后恒为
    ``cause = {code, detail} | null``。控制台因此不需要自己再写一份解析，
    也就不会出现第三个码表。
    """

    if not meta:
        return meta
    normalized = dict(meta)
    raw = meta.get("cause") if meta.get("cause") is not None else meta.get("reason")
    if meta.get("complete") is False:
        normalized["cause"] = InconclusiveReason.from_dict(raw).model_dump(mode="json")
    else:
        normalized["cause"] = None
    return normalized


__all__ = [
    "AgentInfo",
    "CaseCreateRequest",
    "CaseItem",
    "CaseListResponse",
    "CaseRunRequest",
    "CaseRunResponse",
    "ReplayEstimateResponse",
    "ReplayRequest",
    "ReplayResponse",
    "RunDetailResponse",
    "RunDiff",
    "RunListItem",
    "RunListResponse",
    "SuiteCondition",
    "SuiteConditionGroup",
    "SuiteDetailResponse",
    "SuiteItem",
    "SuiteListResponse",
    "SuiteSubmitRequest",
    "SuiteSubmitResponse",
    "SuiteSummary",
    "TimelineResponse",
    "case_to_item",
    "replay_meta_to_dict",
]
