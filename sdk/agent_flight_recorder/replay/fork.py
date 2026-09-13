"""分叉检测。

第一个行为不同的步骤就是"Agent 从这里开始走上另一条路"，它是 Diff 视图的锚点。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Sequence

from pydantic import BaseModel, Field

from ..models import Event, EventType


class ForkKind(str, Enum):
    STEP_SEQUENCE = "step_sequence"
    TOOL_SEQUENCE = "tool_sequence"
    TOOL_ARGS = "tool_args"
    MODEL_OUTPUT = "model_output"
    ERROR = "error"
    LENGTH = "length"


class Fork(BaseModel):
    kind: ForkKind
    message: str
    parent_seq: int | None = None
    replay_seq: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


BEHAVIORAL = (EventType.MODEL_CALL, EventType.TOOL_CALL, EventType.ERROR)


def behavioral_steps(events: Sequence[Event]) -> list[Event]:
    return [e for e in sorted(events, key=lambda e: e.seq) if e.type in BEHAVIORAL]


def classify_step(parent_event: Event | None, replay_event: Event | None) -> Fork | None:
    """比较父 Run 的一步与回放的一步。相同则返回 None。"""

    if parent_event is None and replay_event is None:
        return None
    if parent_event is None:
        return Fork(
            kind=ForkKind.LENGTH,
            message="回放比原始运行多出了新的步骤",
            replay_seq=replay_event.seq if replay_event else None,
        )
    if replay_event is None:
        return Fork(
            kind=ForkKind.LENGTH,
            message="回放提前结束，原始运行还有后续步骤",
            parent_seq=parent_event.seq,
        )

    if parent_event.type is not replay_event.type:
        return Fork(
            kind=ForkKind.STEP_SEQUENCE,
            message="步骤类型发生变化",
            parent_seq=parent_event.seq,
            replay_seq=replay_event.seq,
            detail={"parent_type": parent_event.type.value, "replay_type": replay_event.type.value},
        )

    if parent_event.type is EventType.TOOL_CALL:
        return _compare_tool(parent_event, replay_event)

    if parent_event.type is EventType.MODEL_CALL:
        return _compare_model(parent_event, replay_event)

    return _compare_error(parent_event, replay_event)


def _compare_tool(parent: Event, replay: Event) -> Fork | None:
    if (parent.name or "") != (replay.name or ""):
        return Fork(
            kind=ForkKind.TOOL_SEQUENCE,
            message="调用了不同的工具",
            parent_seq=parent.seq,
            replay_seq=replay.seq,
            detail={"parent_tool": parent.name, "replay_tool": replay.name},
        )

    parent_args = (parent.input or {}).get("args")
    replay_args = (replay.input or {}).get("args")
    if parent_args != replay_args:
        return Fork(
            kind=ForkKind.TOOL_ARGS,
            message="同一个工具但参数不同",
            parent_seq=parent.seq,
            replay_seq=replay.seq,
            detail={"tool": parent.name, "parent_args": parent_args, "replay_args": replay_args},
        )

    parent_error = parent.error is not None
    replay_error = replay.error is not None
    if parent_error != replay_error:
        return Fork(
            kind=ForkKind.ERROR,
            message="报错状态不一致",
            parent_seq=parent.seq,
            replay_seq=replay.seq,
        )
    return None


def _compare_model(parent: Event, replay: Event) -> Fork | None:
    parent_text = (parent.output or {}).get("text") or ""
    replay_text = (replay.output or {}).get("text") or ""
    parent_calls = _signature((parent.output or {}).get("tool_calls"))
    replay_calls = _signature((replay.output or {}).get("tool_calls"))

    if parent_text == replay_text and parent_calls == replay_calls:
        return None

    return Fork(
        kind=ForkKind.MODEL_OUTPUT,
        message="模型输出发生变化",
        parent_seq=parent.seq,
        replay_seq=replay.seq,
        detail={
            "parent_tool_calls": parent_calls,
            "replay_tool_calls": replay_calls,
            "text_changed": parent_text != replay_text,
        },
    )


def _compare_error(parent: Event, replay: Event) -> Fork | None:
    parent_type = parent.error.type if parent.error else None
    replay_type = replay.error.type if replay.error else None
    if parent_type == replay_type:
        return None
    return Fork(
        kind=ForkKind.ERROR,
        message="错误类型发生变化",
        parent_seq=parent.seq,
        replay_seq=replay.seq,
        detail={"parent_error": parent_type, "replay_error": replay_type},
    )


def _signature(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []
    result: list[dict[str, Any]] = []
    for call in tool_calls:
        if isinstance(call, dict):
            result.append({"name": call.get("name"), "args": call.get("args") or {}})
    return result


def detect_fork(parent_events: Sequence[Event], replay_events: Sequence[Event]) -> Fork | None:
    """事后按位置对齐两个 Run，找出第一个不同的步骤。

    回放过程中引擎自己知道步骤对应关系，因此用的是更精确的在线版本；
    这个函数用于比较两个互不相关的 Run。
    """

    parent_steps = behavioral_steps(parent_events)
    replay_steps = behavioral_steps(replay_events)

    for index in range(max(len(parent_steps), len(replay_steps))):
        parent_step = parent_steps[index] if index < len(parent_steps) else None
        replay_step = replay_steps[index] if index < len(replay_steps) else None
        fork = classify_step(parent_step, replay_step)
        if fork is not None:
            return fork
    return None

