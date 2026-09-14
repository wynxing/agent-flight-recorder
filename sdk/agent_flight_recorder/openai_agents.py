"""OpenAI Agents SDK 接入层（录制侧）。

两个框架给的钩子完全不同，因此这里不是 LangChain 那份中间件的改名版：

| 面向 | LangChain / LangGraph | OpenAI Agents SDK |
| --- | --- | --- |
| 模型调用 | AgentMiddleware.wrap_model_call | 实现 Model 协议（get_response） |
| 工具调用 | AgentMiddleware.wrap_tool_call | 替换 FunctionTool.on_invoke_tool |
| 步骤边界 | before_model(state) 回调 | 模型调用的 input items 就是那一刻的状态 |
| 失败语义 | 异常原样冒泡 | 工具异常默认被转成给模型看的文本 |

最后一行是第二个框架接入时才发现的一处真实差异：Agents SDK 默认把工具里的异常
default_tool_error_function 成一句「An error occurred while running the tool」，
交给模型继续跑。回放引擎用异常表达「停在这里」，如果不换掉这条失败通道，
那次停止会被静默吃成一次正常结束。

记录下来的东西仍然是同一套协议事件：model_call / tool_call / state_snapshot，
因此平台侧不需要为第二个框架再加一套时间线。
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from typing import Any, Sequence

from agents import Agent
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tool import FunctionTool

from .cost import estimate_cost
from .identifiers import model_identifier
from .models import (
    ATTR_NODE,
    EffectSource,
    ErrorInfo,
    EventType,
    SideEffect,
    utcnow,
)
from .recorder import Recorder
from .serialization import to_jsonable
from .side_effects import afr_tool, resolve_side_effect

#: 与 LangChain 侧同一个上限：录制的条目数超过它时同时记下真实条数，
#: 回放引擎据此判定「上下文被截断」（见 ReplaySession.validate_recording）。
MAX_ITEMS = 80
NEWLINE = chr(10)

ITEM_FUNCTION_CALL = "function_call"
ITEM_MESSAGE = "message"

#: Agents SDK 把工具里的异常转换成给模型看的文本时，会在 ToolContext 上留下这个标记。
#: 它是 SDK 的内部面（0.22.2），因此这里显式引用并配了会真变红的测试：SDK 改动内部实现
#: 时，那条测试会告诉我们「失败没有再被识别」，而不是让失败静默变成一次成功。
DEFAULT_FAILURE_MARKER = "_function_tool_default_failure_handled"


def sdk_converted_a_failure(ctx: Any) -> bool:
    """这一次工具调用返回的，是不是「框架把异常转成文本」的产物。

    这是第二个框架与 LangGraph 的一处真实差异：LangGraph 的工具异常会冒到中间件上，
    因此失败可以被如实记下；Agents SDK 默认先把它变成一句给模型看的话，再交给循环。
    调用方（录制与回放）据此把「失败」记成失败，而不是当成一次正常的工具输出。
    """

    return bool(getattr(ctx, DEFAULT_FAILURE_MARKER, False))


def converted_tool_error(output: Any) -> ErrorInfo:
    """被框架转换掉的失败：成因类型沿用协议里既有的 ToolError，正文是那句话。"""

    text = output if isinstance(output, str) else json_text(output)
    return ErrorInfo(type="ToolError", message=text)


class ToolCallIndex:
    """call_id -> 产出这个调用的模型事件的 seq。

    与 LangChain 侧同一目的：工具事件要挂在「是哪一个模型步骤让它发生的」上，
    否则时间线上工具与模型调用是散的。
    """

    def __init__(self) -> None:
        self._seq_by_call_id: dict[str, int] = {}

    def remember(self, tool_calls: Sequence[dict[str, Any]], seq: int) -> None:
        for call in tool_calls:
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                self._seq_by_call_id[call_id] = seq

    def parent_seq(self, call_id: Any) -> int | None:
        if not isinstance(call_id, str):
            return None
        return self._seq_by_call_id.get(call_id)


def resolve_model_instance(model: Any) -> Model:
    """把 Agent.model 解析成一个真正的 Model 实例。

    Agents SDK 允许 Agent(model="gpt-4o") 这种写法，名字要经过 ModelProvider
    才会变成具体的 Model。录制与回放都要包住那个 Model，因此这里要先解析出来：
    字符串走 SDK 默认的 MultiProvider（与 Runner 的默认解析一致），
    拿不到具体实例时直接报错，而不是在回放中途才炸。
    """

    if isinstance(model, Model):
        return model
    if isinstance(model, str):
        from agents.models.multi_provider import MultiProvider

        return MultiProvider().get_model(model)
    raise TypeError(
        "Agent.model 必须是 Model 实例或模型名字符串，才能被接管；"
        f"当前是 {type(model).__name__}。请在构建 Agent 时先解析出具体模型。"
    )


class FlightRecorderModel(Model):
    """把每一次模型调用记成 model_call 事件，然后把调用交给真正的模型。

    这里实现的是 Agents SDK 的 Model 协议（get_response / stream_response）：
    这就是它在模型调用上的接管点——没有中间件可挂。
    """

    def __init__(
        self,
        delegate: Any,
        recorder: Recorder,
        *,
        index: ToolCallIndex | None = None,
        capture_state: bool = True,
        capture_items: bool = True,
        node_name: str = "model",
    ) -> None:
        self.delegate = resolve_model_instance(delegate)
        self.recorder = recorder
        self.index = index
        self.capture_state = capture_state
        self.capture_items = capture_items
        self.node_name = node_name

    @property
    def model_name(self) -> str | None:
        """模型的稳定标识（与 LangChain 侧同一套规则）。"""

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
            # 步边界上的「状态」在这个框架里就是这次要喂给模型的条目列表。
            self.recorder.record_state_snapshot(
                {"items": items_of(input), "instructions": system_instructions},
                **{ATTR_NODE: self.node_name},
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
            self._record_error(exc, started)
            raise
        self._record_response(
            response,
            system_instructions=system_instructions,
            input=input,
            started=started,
        )
        return response

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "Agent Flight Recorder 的 Agents SDK 接入层只接管 get_response（非流式）。"
            "流式的录制与复现语义尚未验证，不做「看起来支持、其实丢事件」的静默降级。"
        )

    # ------------------------------------------------------------ 记录实现

    def _record_response(
        self,
        response: ModelResponse,
        *,
        system_instructions: Any,
        input: Any,
        started: datetime,
    ) -> None:
        try:
            model_name = self.model_name
            tool_calls = tool_calls_of(response)
            tokens = tokens_of(response)
            cost = (
                estimate_cost(model_name, tokens.get("input"), tokens.get("output"))
                if tokens
                else None
            )
            event = self.recorder.record(
                EventType.MODEL_CALL,
                name=model_name,
                input=self._request_payload(system_instructions, input, model_name),
                output={
                    "text": text_of(response),
                    "tool_calls": tool_calls,
                    "finish_reason": finish_reason_of(response),
                    # 框架自己的条目形态原样留下：复现只用 text + tool_calls，
                    # 这一项是给「这到底是哪个 API 返回的」留证据的。
                    "message": to_jsonable(list(response.output or [])),
                },
                tokens=tokens,
                cost_usd=cost,
                effect_source=EffectSource.LIVE,
                started_at=started,
                ended_at=utcnow(),
                duration_ms=_elapsed_ms(started),
                attributes={ATTR_NODE: self.node_name},
            )
            if event is not None and self.index is not None:
                self.index.remember(tool_calls, event.seq)
        except Exception as exc:
            self.recorder._note_error(exc)

    def _record_error(self, exc: BaseException, started: datetime) -> None:
        try:
            self.recorder.record(
                EventType.ERROR,
                name=self.model_name,
                error=_error_info(exc),
                started_at=started,
                ended_at=utcnow(),
                duration_ms=_elapsed_ms(started),
                attributes={ATTR_NODE: self.node_name},
            )
        except Exception as inner:
            self.recorder._note_error(inner)

    def _request_payload(
        self, system_instructions: Any, input: Any, model_name: str | None
    ) -> dict[str, Any]:
        items = items_of(input)
        payload: dict[str, Any] = {
            "model": model_name,
            "system": system_instructions if isinstance(system_instructions, str) else "",
        }
        if self.capture_items:
            payload["messages"] = to_jsonable(items[-MAX_ITEMS:])
            payload["message_count"] = len(items)
        return payload


def instrument_tools(
    tools: Sequence[Any],
    recorder: Recorder,
    *,
    index: ToolCallIndex | None = None,
    tool_side_effects: dict[str, SideEffect] | None = None,
    default_side_effect: SideEffect = SideEffect.READ,
    node_name: str = "tools",
) -> list[Any]:
    """把工具包成「先记录、再执行」的版本。非 FunctionTool 原样返回。"""

    return [
        _instrument_tool(
            tool,
            recorder,
            index=index,
            tool_side_effects=tool_side_effects,
            default_side_effect=default_side_effect,
            node_name=node_name,
        )
        if isinstance(tool, FunctionTool)
        else tool
        for tool in tools
    ]


def instrument_agent(
    agent: Agent,
    recorder: Recorder,
    *,
    tool_side_effects: dict[str, SideEffect] | None = None,
    default_side_effect: SideEffect = SideEffect.READ,
    capture_state: bool = True,
    capture_items: bool = True,
) -> Agent:
    """把录制挂到一个 Agent 上，返回取代它的副本。

    Agents SDK 没有「中间件列表」这种东西，接管点就是 Agent 上的 model 与 tools
    两个字段，因此这里是复制一份 Agent 并替换这两个字段，而不是给谁注册回调。
    原来的 Agent 对象保持不变。
    """

    index = ToolCallIndex()
    model = FlightRecorderModel(
        agent.model,
        recorder,
        index=index,
        capture_state=capture_state,
        capture_items=capture_items,
    )
    tools = instrument_tools(
        list(agent.tools or []),
        recorder,
        index=index,
        tool_side_effects=tool_side_effects,
        default_side_effect=default_side_effect,
    )
    return dataclasses.replace(agent, model=model, tools=tools)


def _instrument_tool(
    tool: FunctionTool,
    recorder: Recorder,
    *,
    index: ToolCallIndex | None,
    tool_side_effects: dict[str, SideEffect] | None,
    default_side_effect: SideEffect,
    node_name: str,
) -> FunctionTool:
    """工具接管的公共实现（录制侧）：先记录这一次调用，再放行。"""

    original = tool.on_invoke_tool
    side_effect = resolve_side_effect(
        tool, tool.name, mapping=tool_side_effects, default=default_side_effect
    )

    async def on_invoke_tool(ctx: Any, arguments: str) -> Any:
        started = utcnow()
        args = parse_arguments(arguments)
        try:
            output = await original(ctx, arguments)
        except Exception as exc:
            _record_tool_failure(
                recorder,
                name=tool.name,
                args=args,
                side_effect=side_effect,
                error=_error_info(exc),
                started=started,
                ctx=ctx,
                index=index,
                node_name=node_name,
            )
            raise
        if sdk_converted_a_failure(ctx):
            # 工具失败，但框架已经把它变成文本交给模型了：Agent 的行为保持不变，
            # 我们只把「这一步失败了」如实记下来（与 LangGraph 侧一致）。
            _record_tool_failure(
                recorder,
                name=tool.name,
                args=args,
                side_effect=side_effect,
                error=converted_tool_error(output),
                started=started,
                ctx=ctx,
                index=index,
                node_name=node_name,
            )
            return output
        _record_tool_result(
            recorder,
            name=tool.name,
            args=args,
            output=output,
            side_effect=side_effect,
            started=started,
            ctx=ctx,
            index=index,
            node_name=node_name,
        )
        return output

    wrapper = dataclasses.replace(tool, on_invoke_tool=on_invoke_tool)
    # dataclasses.replace 只复制 dataclass 字段，而副作用声明挂在对象上（不是字段），
    # 因此必须重新贴一次：否则包过一层之后声明就丢了，回放安全等级会静默回落到默认值。
    afr_tool(side_effect)(wrapper)
    return wrapper


# ---------------------------------------------------------------- 记录实现


def _record_tool_result(
    recorder: Recorder,
    *,
    name: str,
    args: Any,
    output: Any,
    side_effect: SideEffect,
    started: datetime,
    ctx: Any,
    index: ToolCallIndex | None,
    node_name: str,
) -> None:
    """工具执行成功：记一条 tool_call 事件（LIVE）。"""

    try:
        recorder.record(
            EventType.TOOL_CALL,
            name=name,
            input={"args": to_jsonable(args)},
            output={
                "text": output if isinstance(output, str) else json_text(output),
                "content": to_jsonable(output),
            },
            side_effect=side_effect,
            effect_source=EffectSource.LIVE,
            parent_seq=_parent_seq(ctx, index),
            started_at=started,
            ended_at=utcnow(),
            duration_ms=_elapsed_ms(started),
            attributes={
                "tool_call_id": _call_id(ctx),
                ATTR_NODE: node_name,
            },
        )
    except Exception as exc:
        recorder._note_error(exc)


def _record_tool_failure(
    recorder: Recorder,
    *,
    name: str,
    args: Any,
    side_effect: SideEffect,
    error: ErrorInfo,
    started: datetime,
    ctx: Any,
    index: ToolCallIndex | None,
    node_name: str,
) -> None:
    """工具抛异常：失败也记成一条 tool_call 事件（error 字段），不丢信息。"""

    try:
        recorder.record(
            EventType.TOOL_CALL,
            name=name,
            input={"args": to_jsonable(args)},
            error=error,
            side_effect=side_effect,
            effect_source=EffectSource.LIVE,
            parent_seq=_parent_seq(ctx, index),
            started_at=started,
            ended_at=utcnow(),
            duration_ms=_elapsed_ms(started),
            attributes={"tool_call_id": _call_id(ctx), ATTR_NODE: node_name},
        )
    except Exception as inner:
        recorder._note_error(inner)


# ---------------------------------------------------------------- 读取工具


def items_of(input: Any) -> list[Any]:
    """模型请求里的 input：单条字符串也算一个条目，方便统一记录。"""

    if isinstance(input, str):
        return [{"role": "user", "content": input}]
    if isinstance(input, list):
        return list(input)
    return []


def text_of(response: ModelResponse) -> str:
    """响应里的助手文本（可能没有，例如这一轮只产生了一个函数调用）。"""

    parts: list[str] = []
    for item in response.output or []:
        if getattr(item, "type", None) != ITEM_MESSAGE:
            continue
        for block in getattr(item, "content", None) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return NEWLINE.join(parts)


def tool_calls_of(response: ModelResponse) -> list[dict[str, Any]]:
    """响应里的函数调用，统一成协议里的 {id, name, args} 形状。"""

    calls: list[dict[str, Any]] = []
    for item in response.output or []:
        if getattr(item, "type", None) != ITEM_FUNCTION_CALL:
            continue
        call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
        calls.append(
            {
                "id": call_id,
                "name": getattr(item, "name", None),
                "args": parse_arguments(getattr(item, "arguments", None)),
            }
        )
    return calls


def tokens_of(response: ModelResponse) -> dict[str, int] | None:
    """Token 用量。Provider 没给就是 None：0 与「不知道」不是一回事。"""

    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    tokens: dict[str, int] = {}
    for key, attr in (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("total", "total_tokens"),
    ):
        value = getattr(usage, attr, None)
        if isinstance(value, int):
            tokens[key] = value
    return tokens or None


def finish_reason_of(response: ModelResponse) -> str | None:
    """Responses API 的条目里没有 finish_reason，这里按产出形态推导。

    取值与 LangChain 侧对齐（tool_calls / stop）：参与比较的是 text 与 tool_calls，
    finish_reason 只是给人看的，因此只在能确定时给出。
    """

    if any(getattr(item, "type", None) == ITEM_FUNCTION_CALL for item in response.output or []):
        return "tool_calls"
    if text_of(response):
        return "stop"
    return None


def parse_arguments(arguments: Any) -> dict[str, Any]:
    """函数调用参数。协议里的 args 是结构，因此这里把 JSON 字符串解析开。"""

    if isinstance(arguments, dict):
        return to_jsonable(arguments)
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return {"__raw": arguments}
    if isinstance(parsed, dict):
        return to_jsonable(parsed)
    return {"value": to_jsonable(parsed)}


def json_text(value: Any) -> str:
    """把任意值渲染成一段 JSON 文本，失败时退回 str()。"""

    try:
        return json.dumps(to_jsonable(value), ensure_ascii=False, default=str)
    except Exception:
        return str(value)


# ---------------------------------------------------------------- 小工具


def _call_id(ctx: Any) -> Any:
    return getattr(ctx, "tool_call_id", None) or _tool_call_field(ctx, "call_id")


def _parent_seq(ctx: Any, index: ToolCallIndex | None) -> int | None:
    if index is None:
        return None
    call_id = _call_id(ctx)
    return index.parent_seq(call_id) if isinstance(call_id, str) else None


def _tool_call_field(ctx: Any, key: str) -> Any:
    tool_call = getattr(ctx, "tool_call", None)
    if isinstance(tool_call, dict):
        return tool_call.get(key)
    return getattr(tool_call, key, None) if tool_call is not None else None


def _elapsed_ms(started: datetime) -> float:
    return round((utcnow() - started).total_seconds() * 1000.0, 3)


def _error_info(exc: BaseException) -> ErrorInfo:
    import traceback

    return ErrorInfo(
        type=type(exc).__name__,
        message=str(exc),
        stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8000:],
    )


__all__ = [
    "DEFAULT_FAILURE_MARKER",
    "FlightRecorderModel",
    "ToolCallIndex",
    "converted_tool_error",
    "finish_reason_of",
    "instrument_agent",
    "instrument_tools",
    "items_of",
    "parse_arguments",
    "resolve_model_instance",
    "sdk_converted_a_failure",
    "text_of",
    "tokens_of",
    "tool_calls_of",
]
