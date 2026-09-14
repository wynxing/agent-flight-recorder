"""OpenAI Agents SDK 回放适配层。

它把框架无关的 ReplaySession 接到 Agents SDK 的执行循环上。接管点与 LangGraph
完全不同（对照表见 agent_flight_recorder/openai_agents.py）：

* 模型调用 —— 实现 Model 协议（get_response），而不是挂中间件；
* 工具调用 —— 替换 FunctionTool.on_invoke_tool，而不是挂中间件；
* 循环上限 —— Runner.run(max_turns=...)，而不是 config.recursion_limit。

三个分支与 LangGraph 侧逐字对齐：recorded 直接返回录制结果（不产生任何真实调用）、
live 照常执行、dry_run 返回「本应做什么」的合成结果。每次记录都会和父 Run 的对应
步骤比较一次，「第一个行为不同的步骤」同样是回放过程中在线确定的。

这里还有一处框架差异必须显式处理：Agents SDK 默认会把工具里抛出的异常**转换成给
模型看的文本**。回放引擎用异常表达「停在这里」（缺录制结果、预算触顶），如果保留
默认失败通道，那次停止会被静默吃成一次正常结束——回放会给出一个看似成功的结论。
工具失败因此不会以「一句看起来很正常的工具输出」的形式进入回放：执行侧靠
sdk_converted_a_failure 识别它（见 openai_agents.py），记一条 ERROR 事件并让这次回放
失败。而引擎自己的停止决定（缺录制结果、预算触顶）走 replay/boundary.py 的共享收尾
路径，由它从异常链里恢复结构化成因（Agents SDK 会把引擎异常再包一层 UserError）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any, Sequence

from agents import RunConfig, Runner
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tool import FunctionTool
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from ..cost import estimate_cost
from ..identifiers import model_identifier
from ..models import (
    ATTR_GATE,
    ATTR_NODE,
    ATTR_REASON,
    ATTR_SEVERITY,
    ATTR_SYNTHETIC,
    SEVERITY_WARNING,
    EffectMode,
    EffectSource,
    ErrorInfo,
    EventType,
    RunStatus,
    SideEffect,
    TokenUsage,
    utcnow,
)
from ..openai_agents import (
    converted_tool_error,
    finish_reason_of,
    items_of,
    parse_arguments,
    resolve_model_instance,
    sdk_converted_a_failure,
    tokens_of,
    tool_calls_of,
)
from ..openai_agents import (
    text_of as response_text_of,
)
from ..recorder import Recorder
from ..serialization import to_jsonable
from ..side_effects import afr_tool, resolve_side_effect
from .boundary import (
    dry_run_text,
    finish_failed_replay,
    missing_recorded_result_cause,
    step_attributes,
)
from .effects import RecordedModelResponse, RecordedToolResult
from .engine import (
    ReplayExhaustedError,
    ReplayResult,
    ReplaySession,
    StepPlan,
    ensure_replay_context,
    plan_summary,
)

#: 与 LangChain 侧同一个上限：录制的条目数超过它时同时记下真实条数。
MAX_ITEMS = 80
NEWLINE = chr(10)


class ReplayToolFailure(RuntimeError):
    """真实执行的工具失败了。

    Agents SDK 默认把工具异常变成一句给模型看的文本，因此这里能拿到的就是那句话；
    语义与 LangGraph 侧一致：工具失败 = 这次回放失败，而不是让模型拿着错误文本继续走。
    """


#: 循环上限。LangGraph 侧的对应物是 recursion_limit（50）：两者都只是「别转不出来」，
#: 不是预算——预算在 ReplaySession 的账本里，与框架无关。
DEFAULT_MAX_TURNS = 20

AgentFactory = Any


class ReplayModel(Model):
    """按 ReplaySession 的指令接管模型调用。"""

    def __init__(
        self,
        *,
        delegate: Model,
        session: ReplaySession,
        recorder: Recorder,
        capture_state: bool = True,
    ) -> None:
        self.delegate = delegate
        self.session = session
        self.recorder = recorder
        self.capture_state = capture_state

    @property
    def model_name(self) -> str | None:
        return model_identifier(self.delegate)

    async def get_response(
        self,
        system_instructions: Any,
        input: Any,
        model_settings: Any,
        tools: Any,
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        *,
        previous_response_id: Any,
        conversation_id: Any,
        prompt: Any,
    ) -> ModelResponse:
        if self.capture_state:
            # 与 LangGraph 侧同名同义：checkpoint 是事件日志在步边界的投影。
            self.recorder.record_state_snapshot(
                {"items": items_of(input), "instructions": system_instructions},
                **{ATTR_NODE: "replay.before_model"},
            )

        plan = self.session.next_step(EventType.MODEL_CALL.value)
        if plan.mode is not EffectMode.LIVE:
            return self._replayed_response(
                plan, system_instructions=system_instructions, input=input
            )

        started = utcnow()
        try:
            response = await self.delegate.get_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt,
            )
        except Exception as exc:
            self._record_failure(exc, started, plan)
            raise
        self._record_live_response(
            response, system_instructions=system_instructions, input=input, started=started, plan=plan
        )
        return response

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "回放适配层只接管 get_response（非流式）。Runner.run_streamed 的复现语义尚未验证，"
            "不做「看起来支持、其实丢事件」的静默降级。"
        )

    # ------------------------------------------------------------ 复现

    def _replayed_response(
        self, plan: StepPlan, *, system_instructions: Any, input: Any
    ) -> ModelResponse:
        recorded = self.session.recorded_model_response(plan.parent_seq)
        items = response_items(recorded)
        tokens = TokenUsage.model_validate(recorded.tokens) if recorded.tokens else None
        event = self.recorder.record(
            EventType.MODEL_CALL,
            name=self.model_name,
            input=self._request_payload(system_instructions, input),
            output={
                "text": recorded.text,
                "tool_calls": recorded.tool_calls,
                "finish_reason": recorded.finish_reason,
                # 复现出来的条目本身，就是这次回放重新构造的框架消息形态。
                "message": to_jsonable(items),
            },
            tokens=tokens,
            cost_usd=recorded.cost_usd,
            effect_source=EffectSource.RECORDED,
            source_seq=plan.parent_seq,
            attributes=step_attributes(plan),
        )
        if event is not None:
            self.session.observe(event, plan.parent_seq)
        return ModelResponse(output=items, usage=usage_of(tokens), response_id=None)

    # ------------------------------------------------------------ 真实执行

    def _record_live_response(
        self,
        response: ModelResponse,
        *,
        system_instructions: Any,
        input: Any,
        started: Any,
        plan: StepPlan,
    ) -> None:
        tokens = tokens_of(response)
        cost = (
            estimate_cost(self.model_name, tokens.get("input"), tokens.get("output"))
            if tokens
            else None
        )
        event = self.recorder.record(
            EventType.MODEL_CALL,
            name=self.model_name,
            input=self._request_payload(system_instructions, input),
            output={
                "text": response_text_of(response),
                "tool_calls": tool_calls(response),
                "finish_reason": finish_reason(response),
                "message": to_jsonable(list(response.output or [])),
            },
            tokens=tokens,
            cost_usd=cost,
            effect_source=EffectSource.LIVE,
            source_seq=plan.parent_seq,
            started_at=started,
            ended_at=utcnow(),
            duration_ms=_elapsed_ms(started),
            attributes=step_attributes(plan),
        )
        if event is not None:
            self.session.observe(event, plan.parent_seq)
        # 调用真的发生了就进账，哪怕这条事件没能被记录器写下去。
        self.session.record_live_model_call(cost)

    def _record_failure(self, exc: BaseException, started: Any, plan: StepPlan) -> None:
        self.recorder.record(
            EventType.ERROR,
            name=self.model_name,
            error=_error_info(exc),
            effect_source=EffectSource.LIVE,
            source_seq=plan.parent_seq,
            started_at=started,
            ended_at=utcnow(),
            duration_ms=_elapsed_ms(started),
        )

    def _request_payload(self, system_instructions: Any, input: Any) -> dict[str, Any]:
        items = items_of(input)
        return {
            "model": self.model_name,
            "system": system_instructions if isinstance(system_instructions, str) else "",
            "messages": to_jsonable(items[-MAX_ITEMS:]),
            "message_count": len(items),
        }


def wrap_tools(
    tools: Sequence[Any],
    *,
    session: ReplaySession,
    recorder: Recorder,
    tool_side_effects: dict[str, SideEffect] | None = None,
    default_side_effect: SideEffect = SideEffect.READ,
) -> list[Any]:
    """把工具包成按回放计划执行的版本。非 FunctionTool 原样返回。"""

    return [
        _wrap_tool(
            tool,
            session=session,
            recorder=recorder,
            tool_side_effects=tool_side_effects,
            default_side_effect=default_side_effect,
        )
        if isinstance(tool, FunctionTool)
        else tool
        for tool in tools
    ]


def _wrap_tool(
    tool: FunctionTool,
    *,
    session: ReplaySession,
    recorder: Recorder,
    tool_side_effects: dict[str, SideEffect] | None,
    default_side_effect: SideEffect,
) -> FunctionTool:
    original = tool.on_invoke_tool
    side_effect = resolve_side_effect(
        tool, tool.name, mapping=tool_side_effects, default=default_side_effect
    )

    async def on_invoke_tool(ctx: Any, arguments: str) -> Any:
        args = parse_arguments(arguments)
        plan = session.next_step(EventType.TOOL_CALL.value, side_effect=side_effect)

        if plan.mode is EffectMode.RECORDED:
            return _replayed_tool(
                tool.name, args, plan, session=session, recorder=recorder, side_effect=side_effect
            )
        if plan.mode is EffectMode.LIVE:
            return await _live_tool(
                original,
                ctx,
                arguments,
                tool.name,
                args,
                plan,
                session=session,
                recorder=recorder,
                side_effect=side_effect,
            )
        return _synthetic_tool(
            tool.name, args, plan, session=session, recorder=recorder, side_effect=side_effect
        )

    wrapper = dataclasses.replace(tool, on_invoke_tool=on_invoke_tool)
    # 与录制侧同一条理由：副作用声明挂在对象上而不是 dataclass 字段上，
    # 复制出来的包装对象必须重新贴一次，否则回放安全等级会静默回落成默认值。
    afr_tool(side_effect)(wrapper)
    # 这里刻意**不改**失败通道。曾经改过（换成「原样抛出」），但实测它并不承重：
    # Agents SDK 把工具失败换成文本时会在 ToolContext 上留标记，执行侧靠
    # sdk_converted_a_failure 就能识别；两条路都指向同一种处置（记失败 + 让回放失败）。
    # 少一条不承重的机制，就少一条未来会被误认为是「这里在管失败语义」的注释。
    # 残余缺口记在 docs/architecture.md 第 8 节：Agent 自己配了 failure_error_function
    # 时，框架把失败当成它自己的取值语义，那一层无法区分。
    return wrapper


def _replayed_tool(
    name: str,
    args: Any,
    plan: StepPlan,
    *,
    session: ReplaySession,
    recorder: Recorder,
    side_effect: SideEffect,
) -> str:
    recorded = session.recorded_tool_result(name, args, parent_seq=plan.parent_seq)
    if recorded is None:
        session.incomplete_reason = missing_recorded_result_cause(name, args)
        raise ReplayExhaustedError(session.incomplete_reason)
    event = recorder.record(
        EventType.TOOL_CALL,
        name=name,
        input={"args": args},
        output={"text": recorded.text, "content": recorded.content},
        side_effect=side_effect,
        effect_source=EffectSource.RECORDED,
        source_seq=recorded.seq,
        attributes=step_attributes(plan),
    )
    if event is not None:
        session.observe(event, plan.parent_seq)
    return recorded_output(recorded)


async def _live_tool(
    original: Any,
    ctx: Any,
    arguments: str,
    name: str,
    args: Any,
    plan: StepPlan,
    *,
    session: ReplaySession,
    recorder: Recorder,
    side_effect: SideEffect,
) -> Any:
    started = utcnow()
    try:
        output = await original(ctx, arguments)
    except Exception as exc:
        recorder.record(
            EventType.ERROR,
            name=name,
            input={"args": args},
            error=_error_info(exc),
            effect_source=EffectSource.LIVE,
            source_seq=plan.parent_seq,
            started_at=started,
            ended_at=utcnow(),
            duration_ms=_elapsed_ms(started),
        )
        raise
    if sdk_converted_a_failure(ctx):
        # 工具抛了，但框架已经把它换成一句给模型看的话。这里不能假装它成功了：
        # 否则回放会带着「工具错误文本」继续跑完，给出一个看似成功的结论。
        error = converted_tool_error(output)
        recorder.record(
            EventType.ERROR,
            name=name,
            input={"args": args},
            error=error,
            effect_source=EffectSource.LIVE,
            source_seq=plan.parent_seq,
            started_at=started,
            ended_at=utcnow(),
            duration_ms=_elapsed_ms(started),
        )
        raise ReplayToolFailure(error.message)
    attributes = step_attributes(plan)
    if plan.reason == "side_effect_executed" and side_effect.is_mutating:
        attributes[ATTR_SEVERITY] = SEVERITY_WARNING
        attributes["side_effect_executed"] = True
    event = recorder.record(
        EventType.TOOL_CALL,
        name=name,
        input={"args": args},
        output={"text": output if isinstance(output, str) else _json_text(output),
                "content": to_jsonable(output)},
        side_effect=side_effect,
        effect_source=EffectSource.LIVE,
        source_seq=plan.parent_seq,
        started_at=started,
        ended_at=utcnow(),
        duration_ms=_elapsed_ms(started),
        attributes=attributes,
    )
    if event is not None:
        session.observe(event, plan.parent_seq)
    return output


def _synthetic_tool(
    name: str,
    args: Any,
    plan: StepPlan,
    *,
    session: ReplaySession,
    recorder: Recorder,
    side_effect: SideEffect,
) -> str:
    """被策略拦下的步骤：返回「本应做什么」，并标成合成结果。"""

    text = dry_run_text(name, args)
    reason = plan.reason or "dry_run"
    attributes = step_attributes(plan)
    attributes.update(
        {
            ATTR_SYNTHETIC: True,
            ATTR_REASON: reason,
            ATTR_SEVERITY: SEVERITY_WARNING,
        }
    )
    if reason == "side_effect_gate":
        attributes[ATTR_GATE] = "side_effect_gate"
    event = recorder.record(
        EventType.TOOL_CALL,
        name=name,
        input={"args": args},
        output={"text": text, "content": text},
        side_effect=side_effect,
        effect_source=EffectSource.DRY_RUN,
        source_seq=plan.parent_seq,
        attributes=attributes,
    )
    if event is not None:
        session.observe(event, plan.parent_seq)
    return text


# ---------------------------------------------------------------- Agent 驱动


def run_replay(
    *,
    session: ReplaySession,
    recorder: Recorder,
    agent_factory: AgentFactory,
    tool_side_effects: dict[str, SideEffect] | None = None,
    default_side_effect: SideEffect = SideEffect.READ,
    max_turns: int | None = DEFAULT_MAX_TURNS,
    initial_input: Any = None,
    tracing_disabled: bool = True,
) -> ReplayResult:
    """同步入口。

    Agents SDK 的执行是异步的（Runner.run），而回放引擎与 LangGraph 适配层都是同步
    调用契约，因此这里提供一个把二者接起来的同步外壳：没有事件循环时自己开一个，
    已经身处事件循环里则明确要求改用 run_replay_async，而不是偷偷起线程。
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running = False
    else:
        running = True
    if running:
        raise RuntimeError(
            "当前线程已有运行中的事件循环：Agents SDK 的回放请改用 run_replay_async。"
        )
    return asyncio.run(
        run_replay_async(
            session=session,
            recorder=recorder,
            agent_factory=agent_factory,
            tool_side_effects=tool_side_effects,
            default_side_effect=default_side_effect,
            max_turns=max_turns,
            initial_input=initial_input,
            tracing_disabled=tracing_disabled,
        )
    )


async def run_replay_async(
    *,
    session: ReplaySession,
    recorder: Recorder,
    agent_factory: AgentFactory,
    tool_side_effects: dict[str, SideEffect] | None = None,
    default_side_effect: SideEffect = SideEffect.READ,
    max_turns: int | None = DEFAULT_MAX_TURNS,
    initial_input: Any = None,
    tracing_disabled: bool = True,
) -> ReplayResult:
    """驱动一次回放。

    agent_factory 的契约（由调用方实现，通常就是它平时构建 Agent 的那个函数）::

        factory(*, model=None, system_prompt=None) -> Agent

    model / system_prompt 为 None 时沿用默认值；非 None 表示回归模式给出的覆盖值。
    与 LangGraph 侧不同的是，这里不需要 factory 接受 middleware 参数：接管点是
    Agent 自己的 model 与 tools 字段，适配器直接替换它们（见 FlightRecorderModel）。
    """

    overrides = session.plan.overrides
    agent = agent_factory(model=overrides.model, system_prompt=overrides.system_prompt)
    delegate = resolve_model_instance(agent.model)
    replay_model = ReplayModel(delegate=delegate, session=session, recorder=recorder)
    tools = wrap_tools(
        list(agent.tools or []),
        session=session,
        recorder=recorder,
        tool_side_effects=tool_side_effects,
        default_side_effect=default_side_effect,
    )
    agent = dataclasses.replace(agent, model=replay_model, tools=tools)

    if initial_input is None:
        # 与 LangGraph 侧同一条契约：这是包内自带的 task-only 快捷路径，只有它自己
        # 知道状态没有被真正恢复；调用方真的给了初始输入时不做这个声明。
        recorder.run.metadata["afr_replay_context"] = "task_only"
    recorder.start(task=session.initial_task, input={"replay": plan_summary(session.plan)})

    try:
        session.validate_recording()
        ensure_replay_context(
            initial_state=initial_input,
            parent_metadata=session.parent_run.metadata,
            initial_input=session.initial_input,
        )
        result = await Runner.run(
            agent,
            replay_input(session, initial_input),
            max_turns=max_turns,
            run_config=RunConfig(tracing_disabled=tracing_disabled),
        )
        if session.incomplete_reason:
            raise ReplayExhaustedError(session.incomplete_reason)
    except Exception as exc:  # noqa: BLE001 - 回放失败必须产出可诊断的结果
        return finish_failed_replay(session=session, recorder=recorder, exc=exc)

    final_output = final_text(result)
    recorder.finish(result=final_output)
    return session.to_result(
        run_id=recorder.run_id,
        status=RunStatus.SUCCEEDED.value,
        final_output=final_output,
    )


def replay_input(session: ReplaySession, initial_input: Any) -> Any:
    """这一次回放的输入。没有显式初始输入时退回父 Run 的 task（task-only 快捷路径）。"""

    if initial_input is not None:
        return initial_input
    return session.initial_task or ""


def final_text(result: Any) -> str:
    output = getattr(result, "final_output", None)
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    return str(output)


# ---------------------------------------------------------------- 消息/用量


def response_items(recorded: RecordedModelResponse) -> list[Any]:
    """把录制的模型输出还原成 Agents SDK 的响应条目。

    与 LangGraph 侧的 to_ai_message 是同一件事的两副面孔：那边的载体是 AIMessage，
    这边的载体是 Responses API 的条目列表。参数与调用 id 都沿用录制里的值，
    因此复现出来的工具调用与父 Run 逐字一致。
    """

    items: list[Any] = []
    if recorded.text:
        items.append(_assistant_message(recorded.text))
    for index, call in enumerate(recorded.tool_calls):
        items.append(_function_call(call, index))
    return items


def _assistant_message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="afr_message",
        type="message",
        role="assistant",
        status="completed",
        content=[
            ResponseOutputText(text=text, type="output_text", annotations=[], logprobs=[])
        ],
    )


def _function_call(call: dict[str, Any], index: int) -> ResponseFunctionToolCall:
    call_id = call.get("id") or f"afr_call_{index}"
    return ResponseFunctionToolCall(
        id=f"afr_item_{index}",
        call_id=call_id,
        type="function_call",
        name=call.get("name") or "",
        arguments=json.dumps(call.get("args") or {}, ensure_ascii=False),
    )


def usage_of(tokens: TokenUsage | None) -> Usage:
    """复现的模型调用继承父 Run 的 token 记账：同一次调用，账目应当一致。"""

    if tokens is None:
        return Usage()
    total = tokens.total
    if total is None:
        total = (tokens.input or 0) + (tokens.output or 0)
    return Usage(input_tokens=tokens.input or 0, output_tokens=tokens.output or 0, total_tokens=total)


def recorded_output(recorded: RecordedToolResult) -> str:
    """工具结果交回给框架时的形态。

    Responses API 的工具输出是文本，因此这里把录制的内容渲染成字符串；录制事件里
    仍然保留原始的 content 结构，回放与父 Run 的对比因此不受这一层渲染影响。
    """

    if isinstance(recorded.content, str):
        return recorded.content
    if recorded.content is not None:
        return _json_text(recorded.content)
    return recorded.text or ""


def tool_calls(response: ModelResponse) -> list[dict[str, Any]]:
    """真实响应里的函数调用，形状与录制侧一致。"""

    return tool_calls_of(response)


def finish_reason(response: ModelResponse) -> str | None:
    return finish_reason_of(response)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(to_jsonable(value), ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 只是展示用
        return str(value)


def _elapsed_ms(started: Any) -> float:
    return round((utcnow() - started).total_seconds() * 1000.0, 3)


def _error_info(exc: BaseException) -> ErrorInfo:
    import traceback

    return ErrorInfo(
        type=type(exc).__name__,
        message=str(exc),
        stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8000:],
    )


__all__ = [
    "DEFAULT_MAX_TURNS",
    "ReplayModel",
    "ReplayToolFailure",
    "final_text",
    "recorded_output",
    "replay_input",
    "response_items",
    "run_replay",
    "run_replay_async",
    "usage_of",
    "wrap_tools",
]
