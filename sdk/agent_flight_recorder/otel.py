"""把录制事件映射成 OTel GenAI 语义约定。

只做导出，不做导入：平台的存储格式是自己的协议，但导出成行业标准能让数据不被
锁在孤岛里。刻意不依赖 opentelemetry 包 —— 这里产出的是普通 dict，任何消费方
都能接。
"""

from __future__ import annotations

from typing import Any, Sequence

from .models import Event, EventType, RunRecord


def events_to_genai_spans(run: RunRecord, events: Sequence[Event]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda e: e.seq):
        span = _span_for(run, event)
        if span is not None:
            spans.append(span)
    return spans


def _span_for(run: RunRecord, event: Event) -> dict[str, Any] | None:
    base: dict[str, Any] = {
        "trace_id": run.id,
        "afr.seq": event.seq,
        "afr.run_id": run.id,
        "start_time": event.started_at.isoformat(),
        "end_time": (event.ended_at or event.started_at).isoformat(),
    }
    if event.effect_source is not None:
        base["afr.effect_source"] = event.effect_source.value
    if event.side_effect is not None:
        base["afr.side_effect"] = event.side_effect.value

    if event.type is EventType.MODEL_CALL:
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.agent.name": run.agent_name,
            "gen_ai.request.model": event.name,
            "gen_ai.response.model": event.name,
        }
        if event.tokens:
            attributes["gen_ai.usage.input_tokens"] = event.tokens.input
            attributes["gen_ai.usage.output_tokens"] = event.tokens.output
        finish_reason = (event.output or {}).get("finish_reason")
        if finish_reason:
            attributes["gen_ai.response.finish_reasons"] = [finish_reason]
        return {
            **base,
            "name": f"chat {event.name or 'model'}",
            "kind": "CLIENT",
            "attributes": {k: v for k, v in attributes.items() if v is not None},
            "status": _status(event),
        }

    if event.type is EventType.TOOL_CALL:
        tool_attributes: dict[str, Any] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.agent.name": run.agent_name,
            "gen_ai.tool.name": event.name,
            "gen_ai.tool.call.id": event.attributes.get("tool_call_id"),
        }
        return {
            **base,
            "name": f"execute_tool {event.name or 'tool'}",
            "kind": "INTERNAL",
            "attributes": {k: v for k, v in tool_attributes.items() if v is not None},
            "status": _status(event),
        }

    if event.type in (EventType.RUN_STARTED, EventType.RUN_FINISHED):
        return {
            **base,
            "name": f"invoke_agent {run.agent_name}",
            "kind": "INTERNAL",
            "attributes": {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": run.agent_name,
            },
            "status": _status(event),
        }

    return None


def _status(event: Event) -> dict[str, str]:
    if event.error is not None:
        return {"code": "ERROR", "message": event.error.message}
    return {"code": "UNSET"}

