"""Agent Flight Recorder 协议 v1 的数据模型。

见 docs/protocol.md。这些模型是 SDK 与服务端唯一的契约定义：服务端直接复用它们，
因此两端不会各自漂移出一套字段。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1
SDK_NAME = "agent-flight-recorder-python"
SDK_VERSION = "0.1.0"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    """稳定的随机 ID。Run、Event、Case 都用它。"""

    return uuid.uuid4().hex


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    STATE_SNAPSHOT = "state_snapshot"
    ERROR = "error"
    RUN_FINISHED = "run_finished"


class SideEffect(str, Enum):
    """工具的副作用等级。回放安全完全建立在这个声明之上。"""

    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"

    @property
    def is_mutating(self) -> bool:
        return self is not SideEffect.READ


class EffectSource(str, Enum):
    """一次回放中，这一步的结果到底从哪来。UI 必须把后三者与 live 区分显示。"""

    LIVE = "live"
    RECORDED = "recorded"
    DRY_RUN = "dry_run"
    BLOCKED = "blocked"


class EffectMode(str, Enum):
    """EffectPolicy 的取值。"""

    RECORDED = "recorded"
    LIVE = "live"
    DRY_RUN = "dry_run"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


class ReplayPreset(str, Enum):
    """两个预设直接对应文档里的两类诉求，也是 UI 上的两个按钮。"""

    REPRODUCE = "reproduce"
    REGRESS = "regress"


class ReplayBudget(BaseModel):
    """一次回放的硬上限：事先声明，执行中在步边界生效。

    两个维度可以单独声明，也可以一起声明。**「没声明」不等于「上限为 0」**：它是
    「这一维不参与判定」，因此不设上限时回放行为与加入这套能力之前完全一致，复现
    模式（本来就不花模型钱）也绝不会被预算逻辑误判成触顶。

    上限只约束**真实发生的模型调用**。复现模式整条路径都读录制结果，不产生真实调用，
    因此既不花成本也不占用调用次数。
    """

    #: 最大成本（USD，按本地价格表估算）。
    max_cost_usd: float | None = Field(default=None, ge=0)
    #: 最大模型调用次数。
    max_model_calls: int | None = Field(default=None, ge=0)

    @property
    def is_set(self) -> bool:
        """是否声明了至少一个上限。两个都不声明就是「不设上限」。"""

        return self.max_cost_usd is not None or self.max_model_calls is not None


# attributes 里的保留键。
ATTR_CHECKPOINT_REF = "checkpoint_ref"
ATTR_NODE = "node"
ATTR_SEVERITY = "severity"
ATTR_SYNTHETIC = "synthetic"
ATTR_GATE = "side_effect_gate"
ATTR_REASON = "reason"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"


class EffectDecision(BaseModel):
    """一次策略解析的完整结果，包含闸门是否介入。"""

    mode: EffectMode
    downgraded: bool = False
    reason: str | None = None
    warn: bool = False


class EffectPolicy(BaseModel):
    """回放策略。优先级：by_seq > by_kind > default > recorded。"""

    default: EffectMode = EffectMode.RECORDED
    by_kind: dict[str, EffectMode] = Field(default_factory=dict)
    by_seq: dict[int, EffectMode] = Field(default_factory=dict)
    allow_side_effect_execution: bool = False

    @classmethod
    def reproduce(cls) -> "EffectPolicy":
        """复现模式：全部使用录制结果，验证第几步开始偏离。"""

        return cls(default=EffectMode.RECORDED)

    @classmethod
    def regress(cls) -> "EffectPolicy":
        """回归模式：模型真跑，工具沿用录制结果，保证两次运行可比。"""

        return cls(
            default=EffectMode.RECORDED,
            by_kind={EventType.MODEL_CALL.value: EffectMode.LIVE},
        )

    def resolve_mode(self, *, seq: int, kind: str) -> EffectMode:
        if seq in self.by_seq:
            return self.by_seq[seq]
        if kind in self.by_kind:
            return self.by_kind[kind]
        return self.default

    def resolve(
        self,
        *,
        seq: int,
        kind: str,
        side_effect: SideEffect | None = None,
    ) -> EffectDecision:
        """解析这一步实际该怎么执行，并让副作用闸门介入。

        闸门只在工具调用上生效，且只在请求真实执行时介入。复现模式读取录制结果，
        不产生任何真实调用，因此不需要被降级。
        """

        mode = self.resolve_mode(seq=seq, kind=kind)

        if kind != EventType.TOOL_CALL.value or mode is not EffectMode.LIVE:
            return EffectDecision(mode=mode)

        if side_effect is None or not side_effect.is_mutating:
            return EffectDecision(mode=mode)

        if not self.allow_side_effect_execution:
            return EffectDecision(
                mode=EffectMode.DRY_RUN,
                downgraded=True,
                reason="side_effect_gate",
                warn=True,
            )

        # 显式放行真实副作用：允许执行，但必须留痕。
        return EffectDecision(mode=EffectMode.LIVE, warn=True, reason="side_effect_executed")


class TokenUsage(BaseModel):
    input: int | None = None
    output: int | None = None
    total: int | None = None


class ErrorInfo(BaseModel):
    type: str
    message: str
    stack: str | None = None


class Event(BaseModel):
    """append-only 事件。写入后不再修改。"""

    model_config = ConfigDict(extra="allow")

    id: str
    run_id: str
    seq: int
    type: EventType
    parent_seq: int | None = None
    source_seq: int | None = None
    name: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: float | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: ErrorInfo | None = None
    tokens: TokenUsage | None = None
    cost_usd: float | None = None
    side_effect: SideEffect | None = None
    effect_source: EffectSource | None = None
    redactions: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_warning(self) -> bool:
        return self.attributes.get(ATTR_SEVERITY) == SEVERITY_WARNING


class RunRecord(BaseModel):
    """一次 Agent 执行。回放产生的是新 Run，而不是对旧 Run 的修改。"""

    model_config = ConfigDict(extra="allow")

    id: str
    agent_name: str
    agent_version: str | None = None
    model: str | None = None
    status: RunStatus = RunStatus.RUNNING
    parent_run_id: str | None = None
    replay_from_seq: int | None = None
    effect_policy: EffectPolicy | None = None
    prompt_version: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    ended_at: datetime | None = None
    redactions: list[str] = Field(default_factory=list)

    @property
    def is_replay(self) -> bool:
        return self.parent_run_id is not None


class RunSummary(BaseModel):
    """服务端计算的汇总。回放效果计数是刻意保留的：一次回放里有多少步真的跑过，
    不能被 UI 掩盖。"""

    event_count: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    error_count: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_total: int = 0
    cost_usd: float = 0.0
    cost_is_estimate: bool = True
    duration_ms: float | None = None
    effect_counts: dict[str, int] = Field(default_factory=dict)


def summarize_events(events: list[Event]) -> RunSummary:
    summary = RunSummary(event_count=len(events))
    started: datetime | None = None
    ended: datetime | None = None

    for event in events:
        if started is None or event.started_at < started:
            started = event.started_at
        endpoint = event.ended_at or event.started_at
        if ended is None or endpoint > ended:
            ended = endpoint

        if event.type is EventType.MODEL_CALL:
            summary.model_calls += 1
        elif event.type is EventType.TOOL_CALL:
            summary.tool_calls += 1
        elif event.type is EventType.ERROR:
            summary.error_count += 1

        if event.tokens:
            summary.tokens_input += event.tokens.input or 0
            summary.tokens_output += event.tokens.output or 0
            summary.tokens_total += event.tokens.total or 0

        if event.cost_usd:
            summary.cost_usd += event.cost_usd

        if event.effect_source is not None:
            key = event.effect_source.value
            summary.effect_counts[key] = summary.effect_counts.get(key, 0) + 1

    summary.cost_usd = round(summary.cost_usd, 6)
    if started and ended:
        summary.duration_ms = round((ended - started).total_seconds() * 1000.0, 3)
    return summary


class SdkInfo(BaseModel):
    name: str = SDK_NAME
    version: str = SDK_VERSION


class IngestRequest(BaseModel):
    protocol_version: int = PROTOCOL_VERSION
    sdk: SdkInfo = Field(default_factory=SdkInfo)
    run: RunRecord
    events: list[Event] = Field(default_factory=list)


class IngestResponse(BaseModel):
    run_id: str
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    max_seq: int = 0
