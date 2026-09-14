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
from agent_flight_recorder.replay.budget import BudgetUsage
from pydantic import BaseModel, Field

from .assertions import AssertionResult, AssertionSpec
from .case_versions import CANONICALIZATION, definition_digest_of_case
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
    #: **当前**定义的摘要（判据相关那一面，见 case_versions.py）。它与套件版本里的成员
    #: 摘要比对，就能回答「这条用例是不是还是那一版」——不需要自己去逐字段 diff。
    definition_digest: str = ""
    #: 最近一次执行**所用的那一版定义**的摘要。与 definition_digest 不同时，说明这条结论
    #: 是在另一版定义下得出的；存量行（版本化之前跑的）没有它，如实为 None 而不是回填一个。
    #: 这里记的是**有效定义**：用例定义 ⊕ 那次执行显式给出的覆盖（见 case_versions.py）。
    last_definition_digest: str | None = None
    #: 那次执行显式给出的回放覆盖（from_seq / preset / policy / model / system_prompt）。
    #: 空表示**没有覆盖**（那一次就是按用例自己的定义跑的），因此它与 last_definition_digest
    #: 一起才构成完整的前提：摘要说「按什么判的」，这一列说「与用例定义差在哪」。
    #: 存量行（没有记录）与「没有覆盖」的区分靠 last_definition_digest：它为 None 就是没记录。
    last_definition_overrides: dict[str, Any] | None = None
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


class CaseUpdateRequest(BaseModel):
    """用例定义的局部更新：**只改给出的字段**，没给的保持原样。

    这组字段就是用例的定义本身（断言、起点、来源运行、副作用策略与其覆盖）。它刻意不含
    `last_*` 这类执行记账：那些字段属于「跑出来的结果」，不该被外部改写。
    """

    name: str | None = None
    description: str | None = None
    source_run_id: str | None = None
    from_seq: int | None = None
    to_seq: int | None = None
    assertions: list[AssertionSpec] | None = None
    labels: dict[str, str] | None = None
    preset: ReplayPreset | None = None
    policy: EffectPolicy | None = None
    model: str | None = None
    system_prompt: str | None = None


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
    #: **整批**的硬上限（最大成本 / 最大模型调用次数），与单次回放同一套语义。
    #: 两个维度都不给 = 不设上限：此时套件行为与没有这套能力时逐字一致。
    budget: ReplayBudget | None = None


class SuiteSubmitResponse(BaseModel):
    suite_id: str
    status: str = "running"
    total: int
    conditions: list[SuiteCondition] = Field(default_factory=list)
    #: 提交即刻就定下来的版本标识：请求返回时归属已经确定，不必等到批次跑完。
    case_set_version: str | None = None


class SuiteItem(BaseModel):
    """套件里的一格：某条用例在某个条件下的结论。"""

    id: str
    case_id: str
    case_name: str
    #: 这一格归属的用例集版本（提交那一刻固化的定义标识）。历史格子没有它，为 None。
    case_set_version: str | None = None
    condition_key: str
    condition: dict[str, Any] = Field(default_factory=dict)
    status: str
    run_id: str | None = None
    results: list[AssertionResult] = Field(default_factory=list)
    cause: InconclusiveReason | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class SuiteBudgetUsage(BudgetUsage):
    """整批的预算记账：与单次回放共用同一份字段（同一个 BudgetUsage），只多一个计数。

    整批的「已用」是这一批**所有格子真实发生的模型调用**合起来的数字，来源与单次回放
    是同一个账本实现，不是事后再去数一遍事件——那种做法会长出第二套记账口径。

    not_started 是「因批次预算用尽而没跑」的格子数。它们从未执行、没有任何结论，
    因此既不计入 failed，也不计入 inconclusive / error，而是归在 unfinished 一侧。
    """

    not_started: int = 0


class SuiteConditionGroup(BaseModel):
    """一个条件的汇总。

    只有四态计数与该条件自己的可判断率：这里没有任何跨条件的合计分数，分母就是
    该条件自己的 total。

    三个桶互斥且穷尽：total == determinable + undecided + unfinished。
    undecided 只包括**跑过了、但拿不到可信结论**的格子（inconclusive + error）；
    unfinished 是**还没跑完**的格子（pending + running + not_started），它没有任何结论，
    因此不能被算进 undecided——把「还没跑」写成「拿不到结论」是对从未执行过的格子下断言。
    not_started 是 unfinished 里的一个子集：格子从未被启动过（整批预算已用尽），
    它与「还没轮到」（pending）是两回事，界面必须能看出哪些没跑、为什么没跑。
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
    not_started: int = 0
    determinable_rate: float | None = None
    errors: int
    items: list[SuiteItem] = Field(default_factory=list)


class CaseSetDrift(BaseModel):
    """本批提交之后，这一版用例集里的用例还是不是当时那一份。

    它只影响提示，不影响归属：这一批的每一条结论都跑在提交那一刻冻结的定义上，用例后来被
    改成什么样，都不会把已经跑出来的结论改写成另一个定义下的结果。
    """

    #: 定义已经变了的用例。changed + missing + unchanged 恒等于版本里的用例数。
    changed: list[str] = Field(default_factory=list)
    #: 行已经不在了的用例（换库、手工清理）。找不到了就说找不到了，不假装它没变。
    missing: list[str] = Field(default_factory=list)
    unchanged: int = 0


class CaseSetRef(BaseModel):
    """一批结果所属的用例集版本（提交那一刻固化的定义）。"""

    id: str
    canonicalization: str = CANONICALIZATION
    case_count: int = 0
    #: 这版定义是否已固化在库里、可以按 id 反查。正常情况下恒为 True；定义行缺失时如实为
    #: False——那时既不能说「无版本记录」（标识明明在），也不能说定义还在。
    recorded: bool = True
    recorded_at: datetime | None = None
    #: 用例自本批之后有没有被改过。定义行缺失时为 None（无从判断）。
    drift: CaseSetDrift | None = None


class CaseDefinition(BaseModel):
    """一条用例里**判据相关**的那一面：能改变结论的字段（见 case_versions.py）。

    展示用的名字、描述与筛选用的 labels 都不在这里：它们改不出任何一条不同的结论。
    """

    source_run_id: str
    from_seq: int | None = None
    to_seq: int | None = None
    #: 固化时的断言（写入时已按 AssertionSpec 归一化）。用 Any 是因为摘要不该对存量数据的
    #: 形状提要求：认不出的旧断言照原样返回，也不该让一次读取失败。
    assertions: list[Any] = Field(default_factory=list)
    effect_policy: dict[str, Any] = Field(default_factory=dict)


class CaseSetVersionMember(BaseModel):
    case_id: str
    #: 这条用例在这一版里的定义摘要（cs1m:<hex>）。
    digest: str
    definition: CaseDefinition


class CaseSetVersionResponse(BaseModel):
    """按标识取回当时固化的用例定义（旧套件的可反查路径）。"""

    id: str
    canonicalization: str = CANONICALIZATION
    case_count: int = 0
    recorded_at: datetime | None = None
    cases: list[CaseSetVersionMember] = Field(default_factory=list)


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
    #: 整批的预算记账。没声明过整批上限时是 None：不设上限不等于「上限为 0」，
    #: 也不该凭空多出一个「上限：无」的账目。
    budget: SuiteBudgetUsage | None = None
    #: 这批跑的是哪一版用例集。版本化之前创建的批次为 None（无版本记录，不伪造回填）。
    case_set: CaseSetRef | None = None
    groups: list[SuiteConditionGroup] = Field(default_factory=list)


class SuiteEstimateResponse(BaseModel):
    """整批的预估：预计多少次真实模型调用、大概多少成本。

    cost_usd 为 null 就是「无法预估」：只要有一格给不出成本，整批就不报数字，
    也不会把已知的那些加起来冒充整批成本。
    """

    cells: int
    model_calls: int = 0
    cost_usd: float | None = None
    cost_is_estimate: bool = True
    detail: str = ""


class SuiteSummary(BaseModel):
    id: str
    status: str
    created_at: datetime | None = None
    finished_at: datetime | None = None
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    #: 这批是哪一版用例。列表里就要能看出来：跨批次的比较建立在这个归属上。
    case_set_version: str | None = None
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
        # 当前定义的摘要：由这一行的内容算出来（纯函数），因此它永远与页面上看到的定义一致。
        definition_digest=definition_digest_of_case(row),
        # 最近一次结论是在哪一版定义下得出的。存量行没有这一列，如实是 None。
        last_definition_digest=getattr(row, "last_definition_digest", None),
        # 那次执行显式给出的覆盖（没有覆盖、或没有记录时都是 None，两者的区分见上）。
        last_definition_overrides=(
            dict(row.last_definition_overrides)
            if getattr(row, "last_definition_overrides", None)
            else None
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
    "CaseDefinition",
    "CaseItem",
    "CaseListResponse",
    "CaseRunRequest",
    "CaseRunResponse",
    "CaseSetDrift",
    "CaseSetRef",
    "CaseSetVersionMember",
    "CaseSetVersionResponse",
    "CaseUpdateRequest",
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
    "SuiteBudgetUsage",
    "SuiteEstimateResponse",
    "SuiteItem",
    "SuiteListResponse",
    "SuiteSubmitRequest",
    "SuiteSubmitResponse",
    "SuiteSummary",
    "TimelineResponse",
    "case_to_item",
    "replay_meta_to_dict",
]
