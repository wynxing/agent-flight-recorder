"""确定性断言。

刻意只支持确定性断言：引入 LLM Judge 会带进第二个不确定性来源，而本项目当前要
证明的是"失败案例可以被稳定复现与验证"。断言与条件标签一起构成 Case。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from agent_flight_recorder.models import Event, EventType
from pydantic import BaseModel

SUPPORTED_TYPES = (
    "no_error",
    "final_output_contains",
    "final_output_not_contains",
    "final_output_matches",
    "tool_called",
    "tool_not_called",
    "tool_sequence_equals",
    "max_tool_calls",
)


class AssertionSpec(BaseModel):
    type: str
    value: Any = None
    tool: str | None = None
    args_contains: dict[str, Any] | None = None
    note: str = ""

    def describe(self) -> str:
        if self.type == "tool_called":
            extra = f"（参数包含 {self.args_contains}）" if self.args_contains else ""
            return f"调用过工具 {self.tool}{extra}"
        if self.type == "tool_not_called":
            return f"没有调用工具 {self.tool}"
        if self.type == "tool_sequence_equals":
            return f"工具序列等于 {self.value}"
        if self.type == "max_tool_calls":
            return f"工具调用次数不超过 {self.value}"
        if self.type == "no_error":
            return "没有产生错误"
        if self.type == "final_output_contains":
            return f"最终结论包含 {self.value!r}"
        if self.type == "final_output_not_contains":
            return f"最终结论不包含 {self.value!r}"
        if self.type == "final_output_matches":
            return f"最终结论匹配 /{self.value}/"
        return f"{self.type}: {self.value!r}"


class AssertionResult(BaseModel):
    spec: AssertionSpec
    passed: bool
    detail: str = ""


def evaluate_assertions(
    assertions: Sequence[AssertionSpec | dict[str, Any]],
    events: Sequence[Event],
    final_output: str | None,
) -> list[AssertionResult]:
    specs = [
        item if isinstance(item, AssertionSpec) else AssertionSpec.model_validate(item)
        for item in assertions
    ]
    return [evaluate(spec, events, final_output) for spec in specs]


def evaluate(spec: AssertionSpec, events: Sequence[Event], final_output: str | None) -> AssertionResult:
    ordered = list(events)
    tool_events = [e for e in ordered if e.type is EventType.TOOL_CALL]
    text = final_output or ""

    if spec.type == "no_error":
        problems = [e for e in ordered if e.type is EventType.ERROR or e.error is not None]
        if problems:
            first = problems[0]
            reason = first.error.message if first.error else first.type.value
            return AssertionResult(
                spec=spec, passed=False, detail=f"出现 {len(problems)} 个错误，首个：{reason}"
            )
        return AssertionResult(spec=spec, passed=True, detail="无错误")

    if spec.type == "final_output_contains":
        needle = str(spec.value)
        passed = needle.lower() in text.lower()
        return AssertionResult(
            spec=spec, passed=passed, detail="命中" if passed else f"未出现 {needle!r}"
        )

    if spec.type == "final_output_not_contains":
        needle = str(spec.value)
        passed = needle.lower() not in text.lower()
        return AssertionResult(
            spec=spec, passed=passed, detail="未出现" if passed else f"仍然出现 {needle!r}"
        )

    if spec.type == "final_output_matches":
        try:
            matched = re.search(str(spec.value), text, re.S) is not None
        except re.error as exc:
            return AssertionResult(spec=spec, passed=False, detail=f"正则无效：{exc}")
        return AssertionResult(spec=spec, passed=matched, detail="匹配" if matched else "未匹配")

    if spec.type == "tool_called":
        matches = [e for e in tool_events if e.name == spec.tool]
        if spec.args_contains:
            matches = [e for e in matches if _args_contain(e, spec.args_contains)]
        return AssertionResult(
            spec=spec, passed=bool(matches), detail=f"命中 {len(matches)} 次" if matches else "未命中"
        )

    if spec.type == "tool_not_called":
        matches = [e for e in tool_events if e.name == spec.tool]
        return AssertionResult(
            spec=spec, passed=not matches, detail="未调用" if not matches else f"被调用了 {len(matches)} 次"
        )

    if spec.type == "tool_sequence_equals":
        expected = [str(item) for item in (spec.value or [])]
        actual = [e.name for e in tool_events]
        passed = expected == actual
        return AssertionResult(
            spec=spec, passed=passed, detail="序列一致" if passed else f"期望 {expected}，实际 {actual}"
        )

    if spec.type == "max_tool_calls":
        limit = int(spec.value)
        passed = len(tool_events) <= limit
        return AssertionResult(
            spec=spec, passed=passed, detail=f"实际 {len(tool_events)} 次，上限 {limit}"
        )

    return AssertionResult(spec=spec, passed=False, detail=f"未知断言类型 {spec.type!r}")


def _args_contain(event: Event, expected: dict[str, Any]) -> bool:
    args = (event.input or {}).get("args") or {}
    if not isinstance(args, dict):
        return False
    for key, value in expected.items():
        if key not in args:
            return False
        actual = args[key]
        if isinstance(value, str) and isinstance(actual, str):
            if value.lower() not in actual.lower():
                return False
        elif actual != value:
            return False
    return True

