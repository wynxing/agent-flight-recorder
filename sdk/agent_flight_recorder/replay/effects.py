"""父 Run 中可被复用的效果。

复现模式的确定性就建立在"按 (工具名 + 参数) 精确回放"上。匹配不上的时候
必须诚实地报"没有录制结果"，而不是随便找一个同名工具的结果顶上 —— 那会让
调试结论建立在错误数据上。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..models import Event, EventType, SideEffect


def args_key(args: Any) -> str:
    """参数的规范化指纹。键序、数字格式差异不应造成误判为"不同参数"。"""

    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(args)


@dataclass
class RecordedModelResponse:
    seq: int
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    message: dict[str, Any] | None = None
    tokens: dict[str, Any] | None = None
    cost_usd: float | None = None


@dataclass
class RecordedToolResult:
    seq: int
    name: str
    args: dict[str, Any]
    text: str
    content: Any
    is_error: bool
    side_effect: SideEffect | None
    error_message: str | None = None


class RecordedEffects:
    """把父 Run 的事件流索引成"可回放的效果库"。"""

    def __init__(self, events: Sequence[Event]) -> None:
        self._model_by_seq: dict[int, RecordedModelResponse] = {}
        self._tool_by_seq: dict[int, RecordedToolResult] = {}
        self._tool_index: dict[tuple[str, str], list[int]] = {}
        self._used: set[int] = set()

        for event in sorted(events, key=lambda e: e.seq):
            if event.type is EventType.MODEL_CALL:
                self._model_by_seq[event.seq] = RecordedModelResponse(
                    seq=event.seq,
                    text=_text(event.output),
                    tool_calls=_tool_calls(event.output),
                    finish_reason=(event.output or {}).get("finish_reason"),
                    message=(event.output or {}).get("message"),
                    tokens=event.tokens.model_dump() if event.tokens else None,
                    cost_usd=event.cost_usd,
                )
            elif event.type is EventType.TOOL_CALL:
                name = event.name or ""
                args = ((event.input or {}).get("args")) or {}
                result = self._tool_by_seq.setdefault(
                    event.seq,
                    RecordedToolResult(
                        seq=event.seq,
                        name=name,
                        args=args,
                        text=_text(event.output),
                        content=(event.output or {}).get("content"),
                        is_error=event.error is not None,
                        side_effect=event.side_effect,
                        error_message=event.error.message if event.error else None,
                    ),
                )
                self._tool_index.setdefault((name, args_key(args)), []).append(result.seq)

    # ------------------------------------------------------------------ 模型

    def model_response_at(self, seq: int | None) -> RecordedModelResponse | None:
        if seq is None:
            return None
        return self._model_by_seq.get(seq)

    # ------------------------------------------------------------------ 工具

    def tool_result_at(self, seq: int | None) -> RecordedToolResult | None:
        if seq is None:
            return None
        return self._tool_by_seq.get(seq)

    def find_tool_result(
        self,
        name: str,
        args: Any,
        *,
        preferred_seq: int | None = None,
    ) -> RecordedToolResult | None:
        """按 (工具名, 参数) 找一条尚未被消费的录制结果。

        preferred_seq 命中时优先使用，让复现模式精确回到父 Run 的同一步。
        """

        candidates = [
            seq for seq in self._tool_index.get((name, args_key(args)), []) if seq not in self._used
        ]
        if not candidates:
            return None

        chosen = preferred_seq if preferred_seq in candidates else candidates[0]
        self._used.add(chosen)
        return self._tool_by_seq.get(chosen)

    @property
    def consumed(self) -> int:
        return len(self._used)


def _text(output: dict[str, Any] | None) -> str:
    if not output:
        return ""
    value = output.get("text")
    return value if isinstance(value, str) else ""


def _tool_calls(output: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not output:
        return []
    calls = output.get("tool_calls")
    if not isinstance(calls, list):
        return []
    normalized: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        normalized.append(
            {
                "id": call.get("id"),
                "name": call.get("name"),
                "args": call.get("args") or {},
            }
        )
    return normalized
