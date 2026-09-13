"""LangGraph / LangChain 回放适配层。

它把框架无关的 ReplaySession 接到真实的 Agent 循环上：

* 需要 recorded 的步骤直接返回录制结果，不调用 handler，因此不会产生任何真实调用；
* 需要 live 的步骤照常执行；
* 需要 dry_run 的步骤返回"本应做什么"的合成结果。

每次记录都会和父 Run 的对应步骤比较一次，因此"第一个行为不同的步骤"是回放过程中
在线确定的，而不是事后猜出来的。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Sequence

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ..cost import estimate_cost
from ..models import (
    ATTR_GATE,
    ATTR_NODE,
    ATTR_REASON,
    ATTR_SEVERITY,
    ATTR_SYNTHETIC,
    SEVERITY_WARNING,
    EffectMode,
    EffectSource,
    EventType,
    RunStatus,
    SideEffect,
    TokenUsage,
    utcnow,
)
from ..recorder import Recorder
from ..serialization import message_to_dict, text_of, to_jsonable, usage_to_tokens
from ..side_effects import resolve_side_effect
from ..middleware import model_identifier
from .effects import RecordedModelResponse, RecordedToolResult
from .engine import (
    ReplayExhaustedError,
    ReplayPlan,
    ReplayResult,
    ReplaySession,
    StepPlan,
    ensure_replay_context,
    plan_summary,
)

MAX_MESSAGES = 80
NEWLINE = chr(10)


class ReplayMiddleware(AgentMiddleware):
    """按 ReplaySession 的指令接管模型调用与工具调用。"""

    def __init__(
        self,
        session: ReplaySession,
        recorder: Recorder,
        *,
        tool_side_effects: dict[str, SideEffect] | None = None,
        default_side_effect: SideEffect = SideEffect.READ,
        capture_state: bool = True,
    ) -> None:
        super().__init__()
        self.session = session
        self.recorder = recorder
        self.tool_side_effects = dict(tool_side_effects or {})
        self.default_side_effect = default_side_effect
        self.capture_state = capture_state

    # ------------------------------------------------------------ 模型调用

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        plan = self.session.next_step(EventType.MODEL_CALL.value)
        if _is_replayed(plan):
            return self._replayed_model_response(request, plan)

        started = utcnow()
        try:
            response = handler(request)
        except Exception as exc:
            self._record_failure(request, exc, started, plan)
            raise
        self._record_live_model(request, response, started, plan)
        return response

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        plan = self.session.next_step(EventType.MODEL_CALL.value)
        if _is_replayed(plan):
            return self._replayed_model_response(request, plan)

        started = utcnow()
        try:
            response = await handler(request)
        except Exception as exc:
            self._record_failure(request, exc, started, plan)
            raise
        self._record_live_model(request, response, started, plan)
        return response

    # ------------------------------------------------------------ 工具调用

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
        side_effect, _ = self._side_effect_for(request)
        plan = self.session.next_step(EventType.TOOL_CALL.value, side_effect=side_effect)
        if _is_replayed(plan):
            return self._replayed_tool_result(request, plan)

        started = utcnow()
        try:
            result = handler(request)
        except Exception as exc:
            self._record_failure(request, exc, started, plan)
            raise
        self._record_live_tool(request, result, started, plan)
        return result

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
        side_effect, _ = self._side_effect_for(request)
        plan = self.session.next_step(EventType.TOOL_CALL.value, side_effect=side_effect)
        if _is_replayed(plan):
            return self._replayed_tool_result(request, plan)

        started = utcnow()
        try:
            result = await handler(request)
        except Exception as exc:
            self._record_failure(request, exc, started, plan)
            raise
        self._record_live_tool(request, result, started, plan)
        return result

    # ------------------------------------------------------------ 状态边界

    def before_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        if self.capture_state:
            self.recorder.record_state_snapshot(state, **{ATTR_NODE: "replay.before_model"})
        return None

    # ------------------------------------------------------------ 记录

    def _replayed_model_response(self, request: ModelRequest, plan: StepPlan) -> Any:
        from langchain.agents.middleware import ModelResponse

        recorded = self.session.recorded_model_response(plan.parent_seq)
        message = to_ai_message(recorded)
        # 复现的模型调用继承父 Run 的 token 与成本：同一次调用，账目应当一致。
        tokens = TokenUsage.model_validate(recorded.tokens) if recorded.tokens else None
        event = self.recorder.record(
            EventType.MODEL_CALL,
            name=self._model_name(request),
            input=self._model_input(request),
            output={
                "text": recorded.text,
                "tool_calls": recorded.tool_calls,
                "finish_reason": recorded.finish_reason,
                "message": message_to_dict(message),
            },
            tokens=tokens,
            cost_usd=recorded.cost_usd,
            effect_source=EffectSource.RECORDED,
            source_seq=plan.parent_seq,
            attributes=self._base_attributes(plan),
        )
        if event is not None:
            self.session.observe(event, plan.parent_seq)
        return ModelResponse(result=[message])

    def _record_live_model(
        self,
        request: ModelRequest,
        response: Any,
        started: Any,
        plan: StepPlan,
    ) -> None:
        messages = getattr(response, "result", None) or []
        ai_message = messages[-1] if messages else None
        tool_calls = [
            {
                "id": _call_get(call, "id"),
                "name": _call_get(call, "name"),
                "args": to_jsonable(_call_get(call, "args") or {}),
            }
            for call in (getattr(ai_message, "tool_calls", None) or [])
        ]
        tokens = usage_to_tokens(ai_message)
        model_name = self._model_name(request)

        event = self.recorder.record(
            EventType.MODEL_CALL,
            name=model_name,
            input=self._model_input(request),
            output={
                "text": text_of(ai_message),
                "tool_calls": tool_calls,
                "finish_reason": _finish_reason(ai_message),
                "message": message_to_dict(ai_message),
            },
            tokens=tokens,
            cost_usd=estimate_cost(model_name, tokens.get("input"), tokens.get("output")) if tokens else None,
            effect_source=EffectSource.LIVE,
            source_seq=plan.parent_seq,
            started_at=started,
            ended_at=utcnow(),
            duration_ms=_elapsed_ms(started),
            attributes=self._base_attributes(plan),
        )
        if event is not None:
            self.session.observe(event, plan.parent_seq)

    def _replayed_tool_result(self, request: ToolCallRequest, plan: StepPlan) -> ToolMessage:
        tool_call = request.tool_call or {}
        name = tool_call.get("name") or ""
        args = to_jsonable(tool_call.get("args") or {})
        side_effect, _ = self._side_effect_for(request)

        if plan.mode is EffectMode.RECORDED:
            recorded = self.session.recorded_tool_result(name, args, parent_seq=plan.parent_seq)
            if recorded is None:
                self.session.incomplete_reason = self._no_recording_text(name, args)
                raise ReplayExhaustedError(self.session.incomplete_reason)
            message = to_tool_message(recorded, tool_call.get("id"))
            event = self.recorder.record(
                EventType.TOOL_CALL,
                name=name,
                input={"args": args},
                output={"text": recorded.text, "content": recorded.content},
                side_effect=side_effect,
                effect_source=EffectSource.RECORDED,
                source_seq=recorded.seq,
                attributes=self._base_attributes(plan),
            )
            if event is not None:
                self.session.observe(event, plan.parent_seq)
            return message

        return self._synthetic_tool_result(
            request,
            plan,
            source=EffectSource.DRY_RUN,
            text=self._dry_run_text(name, args),
            reason=plan.reason or "dry_run",
            side_effect=side_effect,
        )

    def _synthetic_tool_result(
        self,
        request: ToolCallRequest,
        plan: StepPlan,
        *,
        source: EffectSource,
        text: str,
        reason: str,
        side_effect: SideEffect,
    ) -> ToolMessage:
        tool_call = request.tool_call or {}
        name = tool_call.get("name") or ""
        args = to_jsonable(tool_call.get("args") or {})
        message = ToolMessage(
            content=text,
            tool_call_id=tool_call.get("id") or "afr-synthetic",
            name=name or None,
            status="success",
        )
        attributes = self._base_attributes(plan)
        attributes.update(
            {
                ATTR_SYNTHETIC: True,
                ATTR_REASON: reason,
                ATTR_SEVERITY: SEVERITY_WARNING,
            }
        )
        if reason == "side_effect_gate":
            attributes[ATTR_GATE] = "side_effect_gate"

        event = self.recorder.record(
            EventType.TOOL_CALL,
            name=name,
            input={"args": args},
            output={"text": text, "content": text},
            side_effect=side_effect,
            effect_source=source,
            source_seq=plan.parent_seq,
            attributes=attributes,
        )
        if event is not None:
            self.session.observe(event, plan.parent_seq)
        return message

    def _record_live_tool(
        self,
        request: ToolCallRequest,
        result: Any,
        started: Any,
        plan: StepPlan,
    ) -> None:
        tool_call = request.tool_call or {}
        name = tool_call.get("name") or ""
        side_effect, _ = self._side_effect_for(request)
        attributes = self._base_attributes(plan)

        if plan.reason == "side_effect_executed" and side_effect.is_mutating:
            attributes[ATTR_SEVERITY] = SEVERITY_WARNING
            attributes["side_effect_executed"] = True

        event = self.recorder.record(
            EventType.TOOL_CALL,
            name=name,
            input={"args": to_jsonable(tool_call.get("args") or {})},
            output={
                "text": text_of(result),
                "content": to_jsonable(getattr(result, "content", result)),
            },
            error=None if getattr(result, "status", None) != "error" else _tool_status_error(result),
            side_effect=side_effect,
            effect_source=EffectSource.LIVE,
            source_seq=plan.parent_seq,
            started_at=started,
            ended_at=utcnow(),
            duration_ms=_elapsed_ms(started),
            attributes=attributes,
        )
        if event is not None:
            self.session.observe(event, plan.parent_seq)

    def _record_failure(self, request: Any, exc: BaseException, started: Any, plan: StepPlan) -> None:
        tool_call = getattr(request, "tool_call", None) or {}
        self.recorder.record(
            EventType.ERROR,
            name=tool_call.get("name") or self._model_name(request),
            error=_error_info(exc),
            effect_source=EffectSource.LIVE,
            source_seq=plan.parent_seq,
            started_at=started,
            ended_at=utcnow(),
            duration_ms=_elapsed_ms(started),
        )

    # ------------------------------------------------------------ 辅助

    def _base_attributes(self, plan: StepPlan) -> dict[str, Any]:
        attributes: dict[str, Any] = {ATTR_NODE: "replay"}
        if plan.reason:
            attributes[ATTR_REASON] = plan.reason
        if plan.downgraded:
            attributes[ATTR_GATE] = "side_effect_gate"
            attributes[ATTR_SEVERITY] = SEVERITY_WARNING
        return attributes

    def _side_effect_for(self, request: ToolCallRequest) -> tuple[SideEffect, str | None]:
        tool_call = request.tool_call or {}
        name = tool_call.get("name")
        value = resolve_side_effect(
            getattr(request, "tool", None),
            name,
            mapping=self.tool_side_effects,
            default=self.default_side_effect,
        )
        return value, name

    def _model_name(self, request: ModelRequest) -> str | None:
        return model_identifier(getattr(request, "model", None))

    def _model_input(self, request: ModelRequest) -> dict[str, Any]:
        messages = list(getattr(request, "messages", None) or [])
        return {
            "model": self._model_name(request),
            "system": text_of(getattr(request, "system_message", None)),
            "messages": [message_to_dict(m) for m in messages[-MAX_MESSAGES:]],
            "message_count": len(messages),
        }

    def _dry_run_text(self, name: str, args: Any) -> str:
        head = f"[afr dry-run] 未真实执行 {name}。回放策略拦截了这一步的副作用。"
        body = f"本应执行的操作参数: {json.dumps(args, ensure_ascii=False, default=str)}"
        return head + NEWLINE + body

    def _no_recording_text(self, name: str, args: Any) -> str:
        head = f"[afr] 父 Run 中没有与本次调用匹配的 {name} 录制结果，回放已偏离原始轨迹。"
        body = f"本次调用参数: {json.dumps(args, ensure_ascii=False, default=str)}"
        tail = "如需继续，请改用回归模式让工具真实执行，或检查 Agent 行为为何改变。"
        return NEWLINE.join([head, body, tail])


# ---------------------------------------------------------------- Agent 驱动


AgentFactory = Callable[..., Any]


def run_replay(
    *,
    session: ReplaySession,
    recorder: Recorder,
    agent_factory: AgentFactory,
    middleware: Sequence[Any] = (),
    tool_side_effects: dict[str, SideEffect] | None = None,
    default_side_effect: SideEffect = SideEffect.READ,
    checkpointer: Any = None,
    recursion_limit: int = 50,
    initial_state: Any = None,
    thread_id: str | None = None,
) -> ReplayResult:
    """驱动一次回放。

    agent_factory 的契约（由调用方实现，通常就是它平时构建 Agent 的那个函数）::

        factory(*, model=None, system_prompt=None, middleware=(), checkpointer=None) -> agent

    model / system_prompt 为 None 时沿用默认值；非 None 时使用回归模式给出的覆盖值。
    """

    overrides = session.plan.overrides
    replay_middleware = ReplayMiddleware(
        session,
        recorder,
        tool_side_effects=tool_side_effects,
        default_side_effect=default_side_effect,
    )

    agent = agent_factory(
        model=overrides.model,
        system_prompt=overrides.system_prompt,
        middleware=[replay_middleware, *middleware],
        checkpointer=checkpointer,
    )

    state = initial_state if initial_state is not None else default_initial_state(session)
    if initial_state is None:
        # 这是包内自带的 task-only 快捷路径，只有它自己知道状态没有被真正恢复。
        # 调用方显式给了 initial_state 时不能覆盖这个标记，否则回放的 `afr_replay_context`
        # 会声称状态就是原始状态。
        recorder.run.metadata["afr_replay_context"] = "task_only"
    recorder.start(task=session.initial_task, input={"replay": plan_summary(session.plan)})

    config: dict[str, Any] = {"recursion_limit": recursion_limit}
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}

    try:
        session.validate_recording()
        ensure_replay_context(
            initial_state=initial_state,
            parent_metadata=session.parent_run.metadata,
            initial_input=session.initial_input,
        )
        result = agent.invoke(state, config=config)
        if session.incomplete_reason:
            raise ReplayExhaustedError(session.incomplete_reason)
    except ReplayExhaustedError as exc:
        recorder.record_error(exc, reason="replay_exhausted")
        recorder.finish(status=RunStatus.FAILED)
        return session.to_result(run_id=recorder.run_id, status=RunStatus.FAILED.value,
                                 error=str(exc), complete=False, reason=str(exc))
    except Exception as exc:  # noqa: BLE001 - 回放失败必须产出可诊断的结果
        recorder.record_error(exc)
        recorder.finish(status=RunStatus.FAILED)
        return session.to_result(
            run_id=recorder.run_id,
            status=RunStatus.FAILED.value,
            error=f"{type(exc).__name__}: {exc}",
        )

    final_output = final_text(result)
    recorder.finish(result=final_output)
    return session.to_result(
        run_id=recorder.run_id,
        status=RunStatus.SUCCEEDED.value,
        final_output=final_output,
    )


def default_initial_state(session: ReplaySession) -> dict[str, Any]:
    task = session.initial_task or ""
    return {"messages": [HumanMessage(content=task)]}


def final_text(result: Any) -> str:
    if isinstance(result, dict):
        messages = result.get("messages") or []
        if messages:
            return text_of(messages[-1])
    return text_of(result)


def apply_plan_to_recorder(plan: ReplayPlan, recorder: Recorder) -> Recorder:
    """把回放的父子关系写进 Run 头。

    回放是新的 Run，不是对旧 Run 的修改；父子关系靠 parent_run_id 表达。
    """

    recorder.run.parent_run_id = plan.parent_run_id
    recorder.run.replay_from_seq = plan.from_seq
    recorder.run.effect_policy = plan.policy
    if plan.agent_version:
        recorder.run.agent_version = plan.agent_version
    if plan.prompt_version:
        recorder.run.prompt_version = plan.prompt_version
    return recorder


# ---------------------------------------------------------------- 消息转换


def to_ai_message(recorded: RecordedModelResponse) -> AIMessage:
    tool_calls = [
        {
            "name": call.get("name") or "",
            "args": call.get("args") or {},
            "id": call.get("id") or f"afr_call_{index}",
            "type": "tool_call",
        }
        for index, call in enumerate(recorded.tool_calls)
    ]
    return AIMessage(content=recorded.text, tool_calls=tool_calls)


def to_tool_message(recorded: RecordedToolResult, tool_call_id: Any) -> ToolMessage:
    content = recorded.content if recorded.content is not None else recorded.text
    return ToolMessage(
        content=content,
        tool_call_id=tool_call_id or f"afr_tool_{recorded.seq}",
        name=recorded.name or None,
        status="error" if recorded.is_error else "success",
    )


# ---------------------------------------------------------------- 小工具


def _is_replayed(plan: StepPlan) -> bool:
    return plan.mode is not EffectMode.LIVE


def _call_get(call: Any, key: str) -> Any:
    if isinstance(call, dict):
        return call.get(key)
    return getattr(call, key, None)


def _elapsed_ms(started: Any) -> float:
    return round((utcnow() - started).total_seconds() * 1000.0, 3)


def _finish_reason(message: Any) -> str | None:
    metadata = getattr(message, "response_metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get("finish_reason")
        if isinstance(value, str):
            return value
    return None


def _error_info(exc: BaseException):
    import traceback

    from ..models import ErrorInfo

    return ErrorInfo(
        type=type(exc).__name__,
        message=str(exc),
        stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8000:],
    )


def _tool_status_error(message: Any):
    from ..models import ErrorInfo

    return ErrorInfo(type="ToolError", message=text_of(message) or "tool reported error status")
