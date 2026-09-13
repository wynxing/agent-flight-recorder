"""「无法判断」的成因分类：跨层共享的单一事实来源。

一个成因从它产生的地方（SDK / 服务端 / pi）一路到控制台呈现给用户的那句话，用的都是
这里的取值；任何一层都不得再自造字符串（例如服务端曾经自造的
``replay_recording_loss``，与 SDK 的 ``recording_loss`` 是同一件事的第三个名字）。

分类是**闭集**：``from_code`` 对集合之外取值返回 ``InconclusiveCode.UNKNOWN`` 并把原文
留在 ``detail`` 里，绝不会把一个未知取值当成已知成因。

字段只有两个，职责不可互换：

* ``code`` —— 稳定、可判定的机器取值（``InconclusiveCode`` 之一）；
* ``detail`` —— 给人看的具体信息（哪个工具、哪一步、缺了什么）。

因此 ``"recording_loss"`` 是码，``"missing boundary or event sequence gap"`` 是说明；
把它们写成 ``"码: 英文长句"`` 再由调用方 split 是明确不允许的。

Python 与 TypeScript 的取值对齐清单见 docs/replay-semantics.md 第 8 节。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


class InconclusiveCode(str, Enum):
    """「无法判断」的成因码。取值集合与 pi 的 TypeScript 联合类型逐字一致。"""

    # 录制质量：证据本身不足以支撑一次可信复现。
    INCOMPLETE_RECORDING = "incomplete_recording"
    EVENT_SEQUENCE_GAP = "event_sequence_gap"
    REDACTED_REPLAY_DATA = "redacted_replay_data"
    RECORDING_LOSS = "recording_loss"
    TRUNCATED_CONTEXT = "truncated_context"
    MISSING_RECORDED_RESPONSE = "missing_recorded_response"
    MISSING_INITIAL_STATE = "missing_initial_state"

    # 复现偏离：录下来了，但这次回放走不到原来的轨迹上。
    MODEL_CONTEXT_CHANGED = "model_context_changed"
    FINAL_OUTPUT_CHANGED = "final_output_changed"

    # 执行被策略拦下：这次执行本来就没有真实发生，不是结论不通过。
    SIDE_EFFECT_BLOCKED = "side_effect_blocked"

    # 闭集的兜底取值：集合之外的取值显式落在这里并保留原文。
    UNKNOWN = "unknown"


#: 闭集本身。顺序也是文档与控制台映射的顺序。
CAUSE_CODES: tuple[str, ...] = tuple(code.value for code in InconclusiveCode)

#: 判定为「无法判断」时允许多个成因同时成立，这里给出固定优先级。
#: 录制层面拿不到证据，比「某一步偏离」更根本，因此排在前面。
CAUSE_PRECEDENCE: dict[str, int] = {
    InconclusiveCode.INCOMPLETE_RECORDING.value: 0,
    InconclusiveCode.EVENT_SEQUENCE_GAP.value: 1,
    InconclusiveCode.REDACTED_REPLAY_DATA.value: 2,
    InconclusiveCode.RECORDING_LOSS.value: 3,
    InconclusiveCode.TRUNCATED_CONTEXT.value: 4,
    InconclusiveCode.MISSING_INITIAL_STATE.value: 5,
    InconclusiveCode.MISSING_RECORDED_RESPONSE.value: 6,
    InconclusiveCode.MODEL_CONTEXT_CHANGED.value: 7,
    InconclusiveCode.FINAL_OUTPUT_CHANGED.value: 8,
    InconclusiveCode.SIDE_EFFECT_BLOCKED.value: 9,
    InconclusiveCode.UNKNOWN.value: 10,
}


class InconclusiveReason(BaseModel):
    """一次「无法判断」的完整成因：稳定码 + 给人看的说明。

    这是唯一贯穿 SDK、服务端、pi 与控制台的载体形态；``reason`` 字段曾经是自由字符串，
    全新写入的一律是这个结构。
    """

    model_config = ConfigDict(frozen=True)

    code: str = InconclusiveCode.UNKNOWN.value
    detail: str = ""

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_code(cls, code: Any, detail: str = "") -> "InconclusiveReason":
        """用已知码构造；码不在闭集内时落到 ``unknown`` 并保留原文。"""

        raw = _value_of(code)
        if raw in CAUSE_CODES:
            return cls(code=raw, detail=detail)
        return cls(code=InconclusiveCode.UNKNOWN.value, detail=detail or raw)

    @classmethod
    def from_dict(cls, value: Any) -> "InconclusiveReason":
        """把任意来源（结构、字符串、历史载荷）解析成因。永不抛异常。"""

        if value is None:
            return cls()
        if isinstance(value, InconclusiveReason):
            return value
        if isinstance(value, str):
            return cls.legacy(value)
        if isinstance(value, dict):
            if "code" in value:
                return cls.from_code(value.get("code"), str(value.get("detail") or ""))
            # 结构丢失的历史载荷：整段文本就是原文。
            for key in ("reason", "text", "message", "detail"):
                text = value.get(key)
                if isinstance(text, str) and text:
                    return cls.legacy(text)
            return cls()
        return cls.from_code(value)

    @classmethod
    def legacy(cls, text: Any) -> "InconclusiveReason":
        """把历史自由文本 reason 落成结构化成因（向后兼容的要求）。

        历史数据里既有裸码（``recording_loss``）也有「码 + 冒号 + 英文长句」
        （``incomplete_recording: missing boundary or event sequence gap``），还有整句散文。
        能认出码的认码，认不出的落 ``unknown`` 并**原样保留**，绝不猜成某个已知成因。
        """

        raw = ("" if text is None else str(text)).strip()
        if not raw:
            return cls()
        if raw in CAUSE_CODES:
            return cls(code=raw)

        head, separator, tail = raw.partition(":")
        head = head.strip()
        detail = tail.strip() if separator else ""
        if head in CAUSE_CODES:
            return cls(code=head, detail=detail)
        if head in _LEGACY_CODES:
            # 前缀有歧义时以后半句为准：历史里的 `unsupported_context:` 既可能是
            # "truncated messages"（截断），也可能是 "supply initial_state"（初始状态）。
            # 只按前缀判定会把前者错认成后者，因此更具体的后半句优先。
            specific = _code_in_text(detail) if detail else None
            if specific is not None and specific != _LEGACY_CODES[head]:
                return cls(code=specific, detail=detail)
            return cls(code=_LEGACY_CODES[head], detail=detail)

        known = _code_in_text(raw)
        if known is not None:
            return cls(code=known)
        return cls(code=InconclusiveCode.UNKNOWN.value, detail=raw)

    # ------------------------------------------------------------ 读取

    @property
    def is_known(self) -> bool:
        return self.code != InconclusiveCode.UNKNOWN.value

    def to_text(self) -> str:
        """单行文本形态：给日志、异常消息与旧字段用，不是给调用方判定的。"""

        if not self.detail:
            return self.code
        if self.code == InconclusiveCode.UNKNOWN.value:
            return self.detail
        return f"{self.code}: {self.detail}"

    def __str__(self) -> str:  # pragma: no cover - 仅用于可读性
        return self.to_text()


def most_significant(causes: "list[InconclusiveReason] | tuple[InconclusiveReason, ...]") -> InconclusiveReason | None:
    """从多个同时成立的成因里取出最根本的一个（顺序见 ``CAUSE_PRECEDENCE``）。"""

    known = [cause for cause in causes if cause is not None]
    if not known:
        return None
    return min(known, key=lambda cause: CAUSE_PRECEDENCE.get(cause.code, 99))


# ---------------------------------------------------------------- 小工具


def _value_of(code: Any) -> str:
    if code is None:
        return ""
    value = getattr(code, "value", code)
    return str(value)


#: 历史自由文本里的码与别名。长别名在前：``recording_loss`` 是
#: ``replay_recording_loss`` 的子串，先匹配短的会把服务端旧值认错。
_LEGACY_CODES: dict[str, str] = {
    "replay_recording_loss": InconclusiveCode.RECORDING_LOSS.value,
    "recording_loss": InconclusiveCode.RECORDING_LOSS.value,
    "missing_recorded_response": InconclusiveCode.MISSING_RECORDED_RESPONSE.value,
    "missing_model_response": InconclusiveCode.MISSING_RECORDED_RESPONSE.value,
    "no_recording": InconclusiveCode.MISSING_RECORDED_RESPONSE.value,
    "no recorded response": InconclusiveCode.MISSING_RECORDED_RESPONSE.value,
    "incomplete_recording": InconclusiveCode.INCOMPLETE_RECORDING.value,
    "missing boundary": InconclusiveCode.INCOMPLETE_RECORDING.value,
    "unsupported_or_corrupt_bundle": InconclusiveCode.INCOMPLETE_RECORDING.value,
    "boundary or event sequence gap": InconclusiveCode.EVENT_SEQUENCE_GAP.value,
    "event sequence gap": InconclusiveCode.EVENT_SEQUENCE_GAP.value,
    "sequence gap": InconclusiveCode.EVENT_SEQUENCE_GAP.value,
    "early_end": InconclusiveCode.EVENT_SEQUENCE_GAP.value,
    "redacted_replay_data": InconclusiveCode.REDACTED_REPLAY_DATA.value,
    "redacted": InconclusiveCode.REDACTED_REPLAY_DATA.value,
    "truncated messages": InconclusiveCode.TRUNCATED_CONTEXT.value,
    "truncated": InconclusiveCode.TRUNCATED_CONTEXT.value,
    "unsupported_context": InconclusiveCode.MISSING_INITIAL_STATE.value,
    "model_context_changed": InconclusiveCode.MODEL_CONTEXT_CHANGED.value,
    "final_output_changed": InconclusiveCode.FINAL_OUTPUT_CHANGED.value,
    "side_effect_gate": InconclusiveCode.SIDE_EFFECT_BLOCKED.value,
    "side_effect_blocked": InconclusiveCode.SIDE_EFFECT_BLOCKED.value,
    "side_effect_executed": InconclusiveCode.SIDE_EFFECT_BLOCKED.value,
}


def _code_in_text(text: str) -> str | None:
    """整句散文里认码：只在没有任何更可靠信息时才用，因此别名按长度降序匹配。"""

    for alias in sorted(_LEGACY_CODES, key=len, reverse=True):
        if alias in text.replace("_", "_") or alias.replace("_", " ") in text:
            return _LEGACY_CODES[alias]
    return None


__all__ = [
    "CAUSE_CODES",
    "CAUSE_PRECEDENCE",
    "InconclusiveCode",
    "InconclusiveReason",
    "most_significant",
]
