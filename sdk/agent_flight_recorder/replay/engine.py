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
    ReplayPreset,
    RunRecord,
    SideEffect,
)
from .effects import RecordedEffects, RecordedModelResponse, RecordedToolResult
from .fork import Fork, behavioral_steps, classify_step


class ReplayExhaustedError(RuntimeError):
    """父 Run 已经没有对应的录制步骤，回放无法继续。

    这通常意味着被观测的 Agent 行为已经和当初不同（例如模型换了版本、
    工具返回了不同的结果）。对调试来说这是一条有用的信息，不应该被静默吞掉。
    """


class ReplayOverrides(BaseModel):
    """回归模式下真正变的东西：Prompt 和模型。"""

    system_prompt: str | None = None
    model: str | None = None


class ReplayPlan(BaseModel):
    parent_run_id: str
    from_seq: int
    policy: EffectPolicy = Field(default_factory=EffectPolicy.reproduce)
    overrides: ReplayOverrides = Field(default_factory=ReplayOverrides)
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
    reason: str | None = None


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
        self.incomplete_reason: str | None = None

    def validate_recording(self) -> None:
        events = self.parent_events
        if (not events or events[0].type is not EventType.RUN_STARTED
                or events[-1].type is not EventType.RUN_FINISHED
                or [e.seq for e in events] != list(range(1, len(events) + 1))):
            raise ReplayExhaustedError("incomplete_recording: missing boundary or event sequence gap")
        if self.parent_run.redactions or any(e.redactions for e in events):
            raise ReplayExhaustedError("redacted_replay_data")
        if self.parent_run.metadata.get("afr_recording", {}).get("complete") is False:
            raise ReplayExhaustedError("recording_loss")
        for event in events:
            data = event.input or {}
            if event.type is EventType.MODEL_CALL and data.get("message_count", 0) > len(data.get("messages", [])):
                raise ReplayExhaustedError("unsupported_context: truncated messages")

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

    def recorded_model_response(self, parent_seq: int | None) -> RecordedModelResponse:
        response = self.effects.model_response_at(parent_seq)
        if response is None:
            raise ReplayExhaustedError(
                f"父 Run 没有可用于复现的模型输出（parent_seq={parent_seq}）。"
                "回放已偏离原始轨迹，请改用回归模式让模型真实执行。"
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
        "unsupported_context: supply initial_state or explicitly record task_only context"
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

