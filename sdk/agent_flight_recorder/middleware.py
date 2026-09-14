"""LangChain / LangGraph 接入层。

接入方式是一行：把这个中间件加进 create_agent 的 middleware 列表。
它同时解决了"记录 State"这项要求 —— 状态在每步边界天然可得，不需要额外的
快照机制。

录制路径的安全性由 Recorder 保证：这里不做任何可能抛异常的 I/O。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)

from .cost import estimate_cost
from .identifiers import model_identifier
from .models import (
    ATTR_NODE,
    EffectSource,
    EventType,
    SideEffect,
    utcnow,
)
from .recorder import Recorder
from .serialization import message_to_dict, text_of, to_jsonable, usage_to_tokens
from .side_effects import resolve_side_effect

MAX_MESSAGES = 80


class FlightRecorderMiddleware(AgentMiddleware):
    """把 Agent 的每一次模型调用、工具调用与状态变化写进 Recorder。"""

    def __init__(
        self,
        recorder: Recorder,
        *,
        tool_side_effects: dict[str, SideEffect] | None = None,
        default_side_effect: SideEffect = SideEffect.READ,
        capture_state: bool = True,
        capture_messages: bool = True,
        node_name: str | None = None,
    ) -> None:
        super().__init__()
        self.recorder = recorder
        self.tool_side_effects = dict(tool_side_effects or {})
        self.default_side_effect = default_side_effect
        self.capture_state = capture_state
        self.capture_messages = capture_messages
        self.node_name = node_name
        self._model_seq_by_tool_call: dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 模型调用

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        started = utcnow()
        try:
            response = handler(request)
        except Exception as exc:
            self._record_model_error(request, exc, started)
            raise
        self._record_model_response(request, response, started)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> ModelResponse:
        started = utcnow()
        try:
            response = await handler(request)
        except Exception as exc:
            self._record_model_error(request, exc, started)
            raise
        self._record_model_response(request, response, started)
        return response

    # ------------------------------------------------------------ 工具调用

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        started = utcnow()
        side_effect = self._side_effect_for(request)
        try:
            result = handler(request)
        except Exception as exc:
            self._record_tool_error(request, exc, started, side_effect)
            raise
        self._record_tool_result(request, result, started, side_effect)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        started = utcnow()
        side_effect = self._side_effect_for(request)
        try:
            result = await handler(request)
        except Exception as exc:
            self._record_tool_error(request, exc, started, side_effect)
            raise
        self._record_tool_result(request, result, started, side_effect)
        return result

    # ------------------------------------------------------------ 状态边界

    def before_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        """每轮模型调用前的 state 快照。

        Checkpoint 不是独立能力：它就是事件日志在步边界的投影，这里就是那个投影点。
        """

        if self.capture_state:
            self.recorder.record_state_snapshot(
                state,
                **{ATTR_NODE: self.node_name or "before_model"},
            )
        return None

    # ------------------------------------------------------------ 记录实现

    def _record_model_response(
        self,
        request: ModelRequest,
        response: Any,
        started: datetime,
    ) -> None:
        try:
            model_name = self._model_name(request)
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
            cost = None
            if tokens:
                cost = estimate_cost(model_name, tokens.get("input"), tokens.get("output"))

            output: dict[str, Any] = {
                "text": text_of(ai_message),
                "tool_calls": tool_calls,
                "finish_reason": _finish_reason(ai_message),
            }
            if ai_message is not None:
                output["message"] = message_to_dict(ai_message)

            event = self.recorder.record(
                EventType.MODEL_CALL,
                name=model_name,
                input=self._model_input(request, model_name),
                output=output,
                tokens=tokens,
                cost_usd=cost,
                effect_source=EffectSource.LIVE,
                started_at=started,
                ended_at=utcnow(),
                duration_ms=_elapsed_ms(started),
                attributes={ATTR_NODE: self.node_name or "model"},
            )
            if event is not None:
                self._remember_model_seq(tool_calls, event.seq)
        except Exception as exc:  # noqa: BLE001 - 录制不能影响 Agent
            self.recorder._note_error(exc)

    def _record_model_error(self, request: ModelRequest, exc: BaseException, started: datetime) -> None:
        try:
            self.recorder.record(
                EventType.ERROR,
                name=self._model_name(request),
                error=_error_info(exc),
                started_at=started,
                ended_at=utcnow(),
                duration_ms=_elapsed_ms(started),
                attributes={ATTR_NODE: self.node_name or "model"},
            )
        except Exception as inner:  # noqa: BLE001
            self.recorder._note_error(inner)

    def _record_tool_result(
        self,
        request: ToolCallRequest,
        result: Any,
        started: datetime,
        side_effect: SideEffect,
    ) -> None:
        try:
            tool_call = request.tool_call or {}
            name = tool_call.get("name")
            status = getattr(result, "status", None)
            error = None
            if status == "error":
                error = _error_info_from_tool_message(result)

            output: dict[str, Any] = {
                "text": text_of(result),
                "content": to_jsonable(getattr(result, "content", result)),
            }

            self.recorder.record(
                EventType.TOOL_CALL,
                name=name,
                input={"args": to_jsonable(tool_call.get("args") or {})},
                output=output,
                error=error,
                side_effect=side_effect,
                effect_source=EffectSource.LIVE,
                parent_seq=self._model_seq_for(tool_call.get("id")),
                started_at=started,
                ended_at=utcnow(),
                duration_ms=_elapsed_ms(started),
                attributes={
                    "tool_call_id": tool_call.get("id"),
                    ATTR_NODE: self.node_name or "tools",
                },
            )
        except Exception as exc:  # noqa: BLE001
            self.recorder._note_error(exc)

    def _record_tool_error(
        self,
        request: ToolCallRequest,
        exc: BaseException,
        started: datetime,
        side_effect: SideEffect,
    ) -> None:
        try:
            tool_call = request.tool_call or {}
            self.recorder.record(
                EventType.TOOL_CALL,
                name=tool_call.get("name"),
                input={"args": to_jsonable(tool_call.get("args") or {})},
                error=_error_info(exc),
                side_effect=side_effect,
                effect_source=EffectSource.LIVE,
                parent_seq=self._model_seq_for(tool_call.get("id")),
                started_at=started,
                ended_at=utcnow(),
                duration_ms=_elapsed_ms(started),
                attributes={
                    "tool_call_id": tool_call.get("id"),
                    ATTR_NODE: self.node_name or "tools",
                },
            )
        except Exception as inner:  # noqa: BLE001
            self.recorder._note_error(inner)

    # ------------------------------------------------------------ 辅助

    def _model_name(self, request: ModelRequest) -> str | None:
        return model_identifier(getattr(request, "model", None))

    def _model_input(self, request: ModelRequest, model_name: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_name,
            "system": text_of(getattr(request, "system_message", None)),
        }
        if self.capture_messages:
            messages = list(getattr(request, "messages", None) or [])
            payload["messages"] = [message_to_dict(m) for m in messages[-MAX_MESSAGES:]]
            payload["message_count"] = len(messages)
        tools = getattr(request, "tools", None) or []
        payload["tools"] = [_tool_name(t) for t in tools]
        return payload

    def _side_effect_for(self, request: ToolCallRequest) -> SideEffect:
        tool_call = request.tool_call or {}
        return resolve_side_effect(
            getattr(request, "tool", None),
            tool_call.get("name"),
            mapping=self.tool_side_effects,
            default=self.default_side_effect,
        )

    def _remember_model_seq(self, tool_calls: list[dict[str, Any]], seq: int) -> None:
        with self._lock:
            for call in tool_calls:
                call_id = call.get("id")
                if call_id:
                    self._model_seq_by_tool_call[call_id] = seq

    def _model_seq_for(self, tool_call_id: Any) -> int | None:
        if not tool_call_id:
            return None
        with self._lock:
            return self._model_seq_by_tool_call.get(tool_call_id)


def _call_get(call: Any, key: str) -> Any:
    if isinstance(call, dict):
        return call.get(key)
    return getattr(call, key, None)


def _tool_name(tool: Any) -> str:
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(tool, dict) and isinstance(tool.get("name"), str):
        return tool["name"]
    return type(tool).__name__


def _finish_reason(message: Any) -> str | None:
    metadata = getattr(message, "response_metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get("finish_reason")
        if isinstance(value, str):
            return value
    return None


def _elapsed_ms(started: datetime) -> float:
    return round((utcnow() - started).total_seconds() * 1000.0, 3)


def _error_info(exc: BaseException):
    from .models import ErrorInfo

    return ErrorInfo(type=type(exc).__name__, message=str(exc), stack=_short_traceback(exc))


def _error_info_from_tool_message(message: Any):
    from .models import ErrorInfo

    return ErrorInfo(type="ToolError", message=text_of(message) or "tool reported error status")


def _short_traceback(exc: BaseException) -> str:
    import traceback

    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8000:]
