"""Agent Diff：比较两次 Run。

Diff 是计算视图，不持久化实体。它回答的是那个会掏钱的问题：换模型、换 Prompt
之后，Agent 的行为到底哪里不一样了。
"""

from __future__ import annotations

import difflib
from typing import Any, Sequence

from agent_flight_recorder.models import Event, EventType, RunRecord, RunSummary, summarize_events
from agent_flight_recorder.replay.fork import Fork, detect_fork
from pydantic import BaseModel, Field


class AlignedItem(BaseModel):
    index: int
    status: str  # same | changed | only_a | only_b
    label: str
    a: dict[str, Any] | None = None
    b: dict[str, Any] | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class SummaryDelta(BaseModel):
    field: str
    a: float | int | None = None
    b: float | int | None = None
    delta: float | int | None = None
    hint: str = ""


class RunDiff(BaseModel):
    a_run_id: str
    b_run_id: str
    a_label: str
    b_label: str
    tools: list[AlignedItem]
    models: list[AlignedItem]
    summary: list[SummaryDelta]
    final_output: dict[str, Any]
    first_divergence: Fork | None = None
    verdict: str = ""


def diff_runs(
    a_run: RunRecord,
    a_events: Sequence[Event],
    b_run: RunRecord,
    b_events: Sequence[Event],
) -> RunDiff:
    a_summary = summarize_events(list(a_events))
    b_summary = summarize_events(list(b_events))

    tools = align_tools(a_events, b_events)
    models = align_models(a_events, b_events)
    summary = summary_delta(a_summary, b_summary)
    final_output = _final_output(a_events, b_events)

    return RunDiff(
        a_run_id=a_run.id,
        b_run_id=b_run.id,
        a_label=run_label(a_run),
        b_label=run_label(b_run),
        tools=tools,
        models=models,
        summary=summary,
        final_output=final_output,
        first_divergence=detect_fork(a_events, b_events),
        verdict=_verdict(tools, summary, final_output),
    )


def align_tools(a_events: Sequence[Event], b_events: Sequence[Event]) -> list[AlignedItem]:
    a_tools = [e for e in a_events if e.type is EventType.TOOL_CALL]
    b_tools = [e for e in b_events if e.type is EventType.TOOL_CALL]
    return _align(a_tools, b_tools, _tool_label, _tool_payload, _tool_detail)


def align_models(a_events: Sequence[Event], b_events: Sequence[Event]) -> list[AlignedItem]:
    a_models = [e for e in a_events if e.type is EventType.MODEL_CALL]
    b_models = [e for e in b_events if e.type is EventType.MODEL_CALL]
    return _align(a_models, b_models, _model_label, _model_payload, _model_detail)


def summary_delta(a: RunSummary, b: RunSummary) -> list[SummaryDelta]:
    # 最后一个字段标记"减少是否等于更好"。
    #
    # 调用次数刻意不判定：多调几次工具可能意味着更充分的取证，少调几次也可能意味着
    # 漏查。平台不该在语义模糊的地方替用户下结论，因此那一行只给数字。
    rows: list[tuple[str, float | int | None, float | int | None, bool]] = [
        ("模型调用次数", a.model_calls, b.model_calls, False),
        ("工具调用次数", a.tool_calls, b.tool_calls, False),
        ("错误数", a.error_count, b.error_count, True),
        ("输入 tokens", a.tokens_input, b.tokens_input, True),
        ("输出 tokens", a.tokens_output, b.tokens_output, True),
    ]
    deltas: list[SummaryDelta] = []
    for name, left, right, lower_is_better in rows:
        delta = (right or 0) - (left or 0)
        deltas.append(
            SummaryDelta(
                field=name,
                a=left,
                b=right,
                delta=delta,
                hint=_direction(delta, lower_is_better=lower_is_better),
            )
        )

    duration_delta = (b.duration_ms or 0) - (a.duration_ms or 0)
    deltas.append(
        SummaryDelta(
            field="耗时 (ms)",
            a=_round(a.duration_ms),
            b=_round(b.duration_ms),
            delta=_round(duration_delta),
            hint=_direction(_round(duration_delta) or 0, lower_is_better=True),
        )
    )
    deltas.append(
        SummaryDelta(
            field="估算成本 (USD)",
            a=a.cost_usd,
            b=b.cost_usd,
            delta=round(b.cost_usd - a.cost_usd, 6),
            hint=_direction(round(b.cost_usd - a.cost_usd, 6), lower_is_better=True),
        )
    )
    return deltas


def run_label(run: RunRecord) -> str:
    bits = [run.agent_name]
    if run.model:
        bits.append(run.model)
    if run.is_replay:
        bits.append(f"replay@{run.replay_from_seq}")
    return " · ".join(bits)


def _align(a_events, b_events, label_fn, payload_fn, detail_fn) -> list[AlignedItem]:
    a_labels = [label_fn(e) for e in a_events]
    b_labels = [label_fn(e) for e in b_events]
    matcher = difflib.SequenceMatcher(a=a_labels, b=b_labels, autojunk=False)

    items: list[AlignedItem] = []
    index = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                a_event = a_events[i1 + offset]
                b_event = b_events[j1 + offset]
                detail = detail_fn(a_event, b_event)
                items.append(
                    AlignedItem(
                        index=index,
                        status="changed" if detail else "same",
                        label=a_labels[i1 + offset],
                        a=payload_fn(a_event),
                        b=payload_fn(b_event),
                        detail=detail,
                    )
                )
                index += 1
            continue

        if tag in ("delete", "replace"):
            for offset in range(i2 - i1):
                items.append(
                    AlignedItem(
                        index=index,
                        status="only_a",
                        label=a_labels[i1 + offset],
                        a=payload_fn(a_events[i1 + offset]),
                    )
                )
                index += 1

        if tag in ("insert", "replace"):
            for offset in range(j2 - j1):
                items.append(
                    AlignedItem(
                        index=index,
                        status="only_b",
                        label=b_labels[j1 + offset],
                        b=payload_fn(b_events[j1 + offset]),
                    )
                )
                index += 1

    return items


def _tool_label(event: Event) -> str:
    return event.name or "unknown-tool"


def _tool_payload(event: Event) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "name": event.name,
        "args": (event.input or {}).get("args"),
        "result": _excerpt((event.output or {}).get("text")),
        "effect_source": event.effect_source.value if event.effect_source else None,
        "side_effect": event.side_effect.value if event.side_effect else None,
        "synthetic": bool(event.attributes.get("synthetic")),
        "error": event.error.message if event.error else None,
        "duration_ms": event.duration_ms,
    }


def _tool_detail(a: Event, b: Event) -> dict[str, Any]:
    """只报告行为上的差异。

    effect_source 不参与判定：父 Run 是 live、回放是 recorded，这本就是回放的定义。
    把它算成"变了"会让每一次回放对比都全屏飘红，真正的行为差异反而被淹没。
    两侧的 effect_source 仍然随 payload 一起返回，供 UI 单独标注。
    """

    detail: dict[str, Any] = {}
    if (a.input or {}).get("args") != (b.input or {}).get("args"):
        detail["args_changed"] = True
    if (a.output or {}).get("text") != (b.output or {}).get("text"):
        detail["result_changed"] = True
    if (a.error is None) != (b.error is None):
        detail["error_changed"] = {
            "a": a.error.message if a.error else None,
            "b": b.error.message if b.error else None,
        }
    return detail


def _model_label(event: Event) -> str:
    return event.name or "model"


def _model_payload(event: Event) -> dict[str, Any]:
    output = event.output or {}
    return {
        "seq": event.seq,
        "model": event.name,
        "text": _excerpt(output.get("text")),
        "tool_calls": [call.get("name") for call in (output.get("tool_calls") or [])],
        "tokens": event.tokens.model_dump() if event.tokens else None,
        "effect_source": event.effect_source.value if event.effect_source else None,
        "duration_ms": event.duration_ms,
    }


def _model_detail(a: Event, b: Event) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    if _excerpt((a.output or {}).get("text")) != _excerpt((b.output or {}).get("text")):
        detail["text_changed"] = True
    if _calls(a) != _calls(b):
        detail["tool_calls_changed"] = {"a": _calls(a), "b": _calls(b)}
    return detail


def _calls(event: Event) -> list[str]:
    return [call.get("name") for call in ((event.output or {}).get("tool_calls") or [])]


def _final_output(a_events: Sequence[Event], b_events: Sequence[Event]) -> dict[str, Any]:
    from .storage import final_output_of

    a_text = final_output_of(a_events)
    b_text = final_output_of(b_events)
    return {"a": _excerpt(a_text), "b": _excerpt(b_text), "changed": a_text != b_text}


def _verdict(
    tools: list[AlignedItem],
    summary: list[SummaryDelta],
    final_output: dict[str, Any],
) -> str:
    changed_tools = sum(1 for item in tools if item.status != "same")
    errors = next((item for item in summary if item.field == "错误数"), None)

    parts = ["最终结论发生变化" if final_output.get("changed") else "最终结论一致"]
    parts.append(f"{changed_tools} 个工具步骤存在差异" if changed_tools else "工具调用序列完全一致")
    if errors and (errors.delta or 0) > 0:
        parts.append(f"新增 {errors.delta} 个错误")
    elif errors and (errors.delta or 0) < 0:
        parts.append(f"减少 {abs(errors.delta or 0)} 个错误")
    return "；".join(parts)


def _direction(delta: float, *, lower_is_better: bool = False) -> str:
    if delta == 0:
        return "持平"
    if not lower_is_better:
        return ""
    return "更好" if delta < 0 else "更差"


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _excerpt(value: Any, limit: int = 2000) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...[+{len(value) - limit} chars]"
    return value
