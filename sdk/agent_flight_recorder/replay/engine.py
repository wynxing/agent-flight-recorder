"""回放引擎：框架无关的核心。

这里不认识 LangGraph，也不认识任何具体框架。它只负责三件事：

1. 按固定优先级解析每一步的 effect policy；
2. 提供父 Run 里录制下来的效果；
3. 在线判定"第一个行为不同的步骤"。

框架适配层（例如 replay/langgraph_adapter.py）负责真正驱动一次执行。
这样做的直接好处：真实用户可以在自己的进程里加载历史 Run 并回放，平台不是必需品。
"""

from __future__ import annotations

from typing import Any, Sequence

from pydantic import BaseModel, Field

from ..models import (
    EffectMode,
    EffectPolicy,
    Event,
    EventType,
    ReplayBudget,
    ReplayPreset,
    RunRecord,
    SideEffect,
)
from .budget import STOPPED_BY_COST, STOPPED_BY_MODEL_CALLS, BudgetLedger, BudgetUsage
from .effects import RecordedEffects, RecordedModelResponse, RecordedToolResult
from .fork import Fork, behavioral_steps, classify_step
from .reasons import InconclusiveCode, InconclusiveReason


class ReplayExhaustedError(RuntimeError):
    """父 Run 已经没有对应的录制步骤，回放无法继续。

    这通常意味着被观测的 Agent 行为已经和当初不同（例如模型换了版本、
    工具返回了不同的结果）。对调试来说这是一条有用的信息，不应该被静默吞掉。

    分类只有一条出口：成因是 ``cause.code``（``InconclusiveCode`` 闭集），
    不靠异常类型区分。同一个异常类型要表达很多种成因，按类型分会重新长出一套
    「哪个子类对应哪个成因」的映射，而那正是这套分类要消灭的漂移。
    """

    #: 成因码。默认是闭集兜底 ``unknown``，因此任何一次抛出都必然带一个码。
    code: str = InconclusiveCode.UNKNOWN.value
    #: 给人看的具体信息（哪个工具、哪一步、缺了什么）。散文只放这里。
    detail: str = ""

    def __init__(self, detail: Any = "", *, code: str | None = None) -> None:
        # detail 可以是一段说明，也可以是一个已经结构化的成因（引擎内部沿用同一条路径）。
        if isinstance(detail, InconclusiveReason):
            self.code = detail.code
            self.detail = detail.detail
        else:
            if code is not None:
                self.code = code
            self.detail = str(detail)
        super().__init__(self.cause.to_text())

    @property
    def cause(self) -> InconclusiveReason:
        """结构化成因。调用方判定用 ``cause.code``，不要解析异常文本。"""

        return InconclusiveReason.from_code(self.code, self.detail)


class ReplayOverrides(BaseModel):
    """回归模式下真正变的东西：Prompt 和模型。"""

    system_prompt: str | None = None
    model: str | None = None


class ReplayPlan(BaseModel):
    parent_run_id: str
    from_seq: int
    policy: EffectPolicy = Field(default_factory=EffectPolicy.reproduce)
    overrides: ReplayOverrides = Field(default_factory=ReplayOverrides)
    #: 硬上限。默认一个都不设，因此不声明预算的回放行为与加这套能力之前完全一致。
    budget: ReplayBudget = Field(default_factory=ReplayBudget)
    agent_name: str | None = None
    agent_version: str | None = None
    prompt_version: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def reproduce(cls, parent_run_id: str, from_seq: int, **kwargs: Any) -> "ReplayPlan":
        return cls(
            parent_run_id=parent_run_id,
            from_seq=from_seq,
            policy=EffectPolicy.reproduce(),
            **kwargs,
        )

    @classmethod
    def regress(cls, parent_run_id: str, from_seq: int, **kwargs: Any) -> "ReplayPlan":
        return cls(
            parent_run_id=parent_run_id,
            from_seq=from_seq,
            policy=EffectPolicy.regress(),
            **kwargs,
        )

    @classmethod
    def from_preset(cls, preset: ReplayPreset, parent_run_id: str, from_seq: int, **kwargs: Any):
        if preset is ReplayPreset.REGRESS:
            return cls.regress(parent_run_id, from_seq, **kwargs)
        return cls.reproduce(parent_run_id, from_seq, **kwargs)


class StepPlan(BaseModel):
    """引擎给适配层的单步指令。"""

    parent_seq: int | None = None
    mode: EffectMode
    reason: str | None = None
    warn: bool = False
    downgraded: bool = False


class ReplayResult(BaseModel):
    parent_run_id: str
    from_seq: int
    policy: EffectPolicy
    run_id: str | None = None
    status: str | None = None
    first_fork: Fork | None = None
    forks: list[Fork] = Field(default_factory=list)
    steps: list[StepPlan] = Field(default_factory=list)
    final_output: Any = None
    error: str | None = None
    complete: bool = True
    #: 结构化成因。``None`` 表示这次回放给出了结论；非 ``None`` 时一定是
    #: ``{code, detail}``，不再是一个自由字符串。
    reason: InconclusiveReason | None = None
    #: 旧载荷里自由文本 reason 的原文，仅在做历史兼容解析时保留。
    legacy_reason: str | None = None
    #: 预算记账（已用 / 上限 / 是否触顶）。没有声明上限时为 None：没声明过上限的回放
    #: 不该凭空多出一个「上限：无」的记账对象。
    budget: BudgetUsage | None = None


class ReplaySession:
    """一次回放的运行时状态。

    步骤用**位置**对齐父 Run：第 k 个行为步骤对第 k 个行为步骤。
    "第一个不同的步骤"因此有明确定义，不依赖任何框架的内部结构。
    """

    def __init__(
        self,
        plan: ReplayPlan,
        parent_run: RunRecord,
        parent_events: Sequence[Event],
    ) -> None:
        self.plan = plan
        self.parent_run = parent_run
        self.parent_events = sorted(parent_events, key=lambda e: e.seq)
        self.effects = RecordedEffects(self.parent_events)
        self.reference_steps = behavioral_steps(self.parent_events)
        self._ref_by_seq = {event.seq: event for event in self.reference_steps}
        self._cursor = 0
        self.first_fork: Fork | None = None
        self.forks: list[Fork] = []
        self.steps: list[StepPlan] = []
        #: 预算账本。只记真实发生的模型调用；recorded 复现既不计次也不计费。
        self.ledger = BudgetLedger(self.plan.budget)
        #: 已判定的成因（结构化的）。一旦置上，后续步骤不再解析。
        self.incomplete_reason: InconclusiveReason | None = None

    def validate_recording(self) -> None:
        """录制质量预检：证据不足就显式给出成因，而不是跑出一个看似合理的结论。

        每一条边界的成因码是独立的（边界是不是缺失、seq 有没有缺口、是否被截断，
        本来就是三件不同的事，控制台给出的下一步也不同）。
        """

        events = self.parent_events
        if not events:
            raise ReplayExhaustedError("父 Run 没有任何事件，无从对齐", code=InconclusiveCode.INCOMPLETE_RECORDING.value)
        if events[0].type is not EventType.RUN_STARTED or events[-1].type is not EventType.RUN_FINISHED:
            raise ReplayExhaustedError(
                "父 Run 缺少 run_started / run_finished 边界",
                code=InconclusiveCode.INCOMPLETE_RECORDING.value,
            )
        if [e.seq for e in events] != list(range(1, len(events) + 1)):
            raise ReplayExhaustedError(
                "父 Run 的事件 seq 不连续，说明中间丢过事件",
                code=InconclusiveCode.EVENT_SEQUENCE_GAP.value,
            )
        if self.parent_run.redactions or any(e.redactions for e in events):
            raise ReplayExhaustedError(
                "父 Run 或事件发生过脱敏，证据不再逐字可比",
                code=InconclusiveCode.REDACTED_REPLAY_DATA.value,
            )
        if self.parent_run.metadata.get("afr_recording", {}).get("complete") is False:
            raise ReplayExhaustedError(
                "录制方声明本次录制丢过事件（metadata.afr_recording.complete = false）",
                code=InconclusiveCode.RECORDING_LOSS.value,
            )
        for event in events:
            data = event.input or {}
            if event.type is EventType.MODEL_CALL and data.get("message_count", 0) > len(data.get("messages", [])):
                raise ReplayExhaustedError(
                    "模型输入的消息条数被截断：录制只保留了最近的消息",
                    code=InconclusiveCode.TRUNCATED_CONTEXT.value,
                )

    # ------------------------------------------------------------------ 父 Run

    @property
    def initial_task(self) -> str | None:
        for event in self.parent_events:
            if event.type is EventType.RUN_STARTED:
                task = (event.input or {}).get("task")
                if isinstance(task, str):
                    return task
        return None

    @property
    def initial_input(self) -> dict[str, Any] | None:
        for event in self.parent_events:
            if event.type is EventType.RUN_STARTED:
                value = (event.input or {}).get("input")
                if isinstance(value, dict):
                    return value
        return None

    # ------------------------------------------------------------------ 步骤

    def next_step(self, kind: str, *, side_effect: SideEffect | None = None) -> StepPlan:
        """解析下一个步骤该怎么执行。

        分叉点之前的步骤被强制为 recorded：它们不产生任何真实调用，因此
        "从第 3 步开始回放"不需要重新执行前两步的代价，也不会引入新的不确定性。
        """

        if self.incomplete_reason:
            raise ReplayExhaustedError(self.incomplete_reason)
        reference = (
            self.reference_steps[self._cursor]
            if self._cursor < len(self.reference_steps)
            else None
        )
        self._cursor += 1

        if reference is None:
            decision = self.plan.policy.resolve(seq=0, kind=kind, side_effect=side_effect)
            plan = StepPlan(parent_seq=None, mode=decision.mode,
                            reason=decision.reason or "beyond_parent_tail",
                            warn=decision.warn, downgraded=decision.downgraded)
        elif reference.seq < self.plan.from_seq:
            plan = StepPlan(parent_seq=reference.seq, mode=EffectMode.RECORDED, reason="before_fork_point")
        else:
            decision = self.plan.policy.resolve(seq=reference.seq, kind=kind, side_effect=side_effect)
            plan = StepPlan(
                parent_seq=reference.seq,
                mode=decision.mode,
                reason=decision.reason,
                warn=decision.warn,
                downgraded=decision.downgraded,
            )

        # 预算判定就在步边界上：这一步真要发出去之前先看账，超了就停在这里。
        # 只有真实执行的步骤参与判定——读取录制结果的步骤既不花钱也不占次数，因此
        # 复现模式永远不会被预算逻辑误伤（见 docs/replay-semantics.md 第 9 节）。
        # 已经发出的那次调用允许完成并如实记账，这里不做预测性中断。
        if plan.mode is EffectMode.LIVE:
            stopped_by = self.ledger.exceeded_by()
            if stopped_by is not None:
                self.ledger.stopped_by = stopped_by
                self.incomplete_reason = InconclusiveReason.from_code(
                    InconclusiveCode.BUDGET_EXCEEDED.value,
                    self.budget_stop_detail(stopped_by),
                )
                raise ReplayExhaustedError(self.incomplete_reason)

        self.steps.append(plan)
        return plan

    def observe(self, replay_event: Event, parent_seq: int | None) -> Fork | None:
        """把回放产生的一步与父 Run 的对应步骤比较，记录是否分叉。"""

        reference = self._ref_by_seq.get(parent_seq) if parent_seq is not None else None
        fork = classify_step(reference, replay_event)
        if fork is not None:
            self.forks.append(fork)
            if self.first_fork is None:
                self.first_fork = fork
        return fork

    # ------------------------------------------------------------------ 效果

    def record_live_model_call(self, cost_usd: float | None) -> None:
        """记一次已经完成的真实模型调用。由适配层在调用返回后调用。"""

        self.ledger.note_model_call(cost_usd)

    def budget_usage(self) -> BudgetUsage:
        """当前账目（已用 / 上限 / 是否触顶）。"""

        return self.ledger.usage()

    def budget_stop_detail(self, stopped_by: str) -> str:
        """触顶时的说明：说清停在哪一维、已用多少，并给出下一步。"""

        usage = self.budget_usage()
        if stopped_by == STOPPED_BY_MODEL_CALLS:
            head = (
                f"已达到本次回放的模型调用上限：已用 {usage.model_calls_used} 次"
                f"（上限 {usage.max_model_calls} 次）。"
            )
        elif stopped_by == STOPPED_BY_COST:
            head = (
                f"已达到本次回放的成本上限：已用约 ${usage.cost_used_usd:.6f}"
                f"（上限 ${usage.max_cost_usd}，估算）。"
            )
        else:  # pragma: no cover - 目前只有两个维度
            head = "已达到本次回放的预算上限。"
        return (
            head
            + "回放已在步边界停止，没有继续发起新的模型调用；已经发出的那次调用已如实记账。"
            + "提高上限或缩小回放范围后可以重跑。"
        )

    def recorded_model_response(self, parent_seq: int | None) -> RecordedModelResponse:
        response = self.effects.model_response_at(parent_seq)
        if response is None:
            raise ReplayExhaustedError(
                f"父 Run 没有可用于复现的模型输出（parent_seq={parent_seq}）",
                code=InconclusiveCode.MISSING_RECORDED_RESPONSE.value,
            )
        return response

    def recorded_tool_result(
        self,
        name: str,
        args: Any,
        *,
        parent_seq: int | None,
    ) -> RecordedToolResult | None:
        return self.effects.find_tool_result(name, args, preferred_seq=parent_seq)

    # ------------------------------------------------------------------ 结果

    def to_result(self, **kwargs: Any) -> ReplayResult:
        """构造结果。``cause`` 是结构化成因；``reason`` 若直接传字符串会按兼容路径解析。"""

        cause = kwargs.pop("cause", None)
        legacy = kwargs.pop("reason", None)
        if cause is not None:
            kwargs["reason"] = InconclusiveReason.from_dict(cause)
        elif legacy is not None:
            kwargs["reason"] = InconclusiveReason.from_dict(legacy)
            if isinstance(legacy, str):
                kwargs["legacy_reason"] = legacy
        if "budget" not in kwargs and self.plan.budget.is_set:
            kwargs["budget"] = self.budget_usage()
        return ReplayResult(
            parent_run_id=self.plan.parent_run_id,
            from_seq=self.plan.from_seq,
            policy=self.plan.policy,
            first_fork=self.first_fork,
            forks=self.forks,
            steps=self.steps,
            **kwargs,
        )


def ensure_replay_context(
    *,
    initial_state: Any,
    parent_metadata: dict[str, Any] | None,
    initial_input: dict[str, Any] | None,
) -> None:
    """父 Run 记录了初始 input，就必须把状态真的恢复回来。

    `initial_state` 不是可选优化，而是复现可信度的前提。默认只构造 task 消息的
    快捷路径必须由录制方显式声明 `afr_replay_context = "task_only"`，否则引擎
    不会假装状态已被恢复（见 docs/replay-semantics.md 第 1 节）。
    """

    if initial_state is not None or not initial_input:
        return
    if (parent_metadata or {}).get("afr_replay_context") == "task_only":
        return
    raise ReplayExhaustedError(
        "父 Run 记录了初始 input，但回放既没有拿到 initial_state，也没有 task_only 声明",
        code=InconclusiveCode.MISSING_INITIAL_STATE.value,
    )


def plan_summary(plan: ReplayPlan) -> str:
    """给 UI 用的一句人话。"""

    if plan.policy.by_kind.get(EventType.MODEL_CALL.value) is EffectMode.LIVE:
        mode = "回归模式：模型真实执行，工具沿用录制结果"
    else:
        mode = "复现模式：全部使用录制结果，不产生真实调用"
    if plan.policy.allow_side_effect_execution:
        mode += "（已允许真实副作用，请确认风险）"
    return f"{mode}，从第 {plan.from_seq} 步开始"

