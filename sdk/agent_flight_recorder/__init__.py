"""Agent Flight Recorder SDK —— 记录、回放与调试 AI Agent。

    Replay, debug and evaluate AI agents like software.

协议见 docs/protocol.md，回放语义见 docs/replay-semantics.md。

需要 LangChain / LangGraph 的接入层采用惰性导入，因此核心包只依赖 pydantic：
不用框架的用户也能用 Recorder 与回放引擎。
"""

from __future__ import annotations

from typing import Any

from .client import DEFAULT_ENDPOINT, FailingTransport, HttpTransport, NullTransport
from .cost import estimate_cost
from .models import (
    PROTOCOL_VERSION,
    SDK_NAME,
    SDK_VERSION,
    EffectDecision,
    EffectMode,
    EffectPolicy,
    EffectSource,
    ErrorInfo,
    Event,
    EventType,
    IngestRequest,
    IngestResponse,
    ReplayBudget,
    ReplayPreset,
    RunRecord,
    RunStatus,
    RunSummary,
    SideEffect,
    TokenUsage,
    summarize_events,
)
from .recorder import DoctorReport, Recorder, RecorderStats, doctor, new_id
from .registry import (
    ENTRY_POINT_GROUP,
    AgentSpec,
    SeedCase,
    load_agent_specs,
    resolve_agent_spec,
)
from .replay.reasons import (
    CAUSE_CODES,
    CAUSE_PRECEDENCE,
    InconclusiveCode,
    InconclusiveReason,
    most_significant,
)
from .serialization import message_to_dict, to_json_text, to_jsonable
from .side_effects import AFR_SIDE_EFFECT_ATTR, afr_tool, resolve_side_effect

__version__ = SDK_VERSION

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    # LangChain / LangGraph 接入层
    "FlightRecorderMiddleware": ("middleware", "FlightRecorderMiddleware"),
    # 回放适配层
    "ReplayMiddleware": ("replay.langgraph_adapter", "ReplayMiddleware"),
    "run_replay": ("replay.langgraph_adapter", "run_replay"),
    "apply_plan_to_recorder": ("replay.langgraph_adapter", "apply_plan_to_recorder"),
    "default_initial_state": ("replay.langgraph_adapter", "default_initial_state"),
    # OpenAI Agents SDK 接入层：录制侧接管模型与工具，回放侧在 replay/ 下
    "FlightRecorderModel": ("openai_agents", "FlightRecorderModel"),
    "instrument_agent": ("openai_agents", "instrument_agent"),
    "ReplayModel": ("replay.openai_agents_adapter", "ReplayModel"),
    "run_openai_agents_replay": ("replay.openai_agents_adapter", "run_replay"),
    # OTel 导出
    "events_to_genai_spans": ("otel", "events_to_genai_spans"),
}


def __getattr__(name: str) -> Any:
    """惰性导入依赖可选第三方库的部分。

    这样核心包保持轻依赖，而导入 run_replay 之类的入口依然可用。
    """

    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    module = importlib.import_module(f".{target[0]}", __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "AFR_SIDE_EFFECT_ATTR",
    "CAUSE_CODES",
    "CAUSE_PRECEDENCE",
    "DEFAULT_ENDPOINT",
    "DoctorReport",
    "ENTRY_POINT_GROUP",
    "EffectDecision",
    "EffectMode",
    "EffectPolicy",
    "EffectSource",
    "ErrorInfo",
    "Event",
    "EventType",
    "FailingTransport",
    "FlightRecorderMiddleware",
    "FlightRecorderModel",
    "HttpTransport",
    "IngestRequest",
    "IngestResponse",
    "InconclusiveCode",
    "InconclusiveReason",
    "NullTransport",
    "PROTOCOL_VERSION",
    "Recorder",
    "RecorderStats",
    "ReplayBudget",
    "ReplayMiddleware",
    "ReplayModel",
    "ReplayPreset",
    "RunRecord",
    "RunStatus",
    "RunSummary",
    "SDK_NAME",
    "SDK_VERSION",
    "SeedCase",
    "SideEffect",
    "TokenUsage",
    "AgentSpec",
    "afr_tool",
    "apply_plan_to_recorder",
    "default_initial_state",
    "doctor",
    "estimate_cost",
    "events_to_genai_spans",
    "instrument_agent",
    "message_to_dict",
    "new_id",
    "load_agent_specs",
    "most_significant",
    "resolve_agent_spec",
    "resolve_side_effect",
    "run_replay",
    "run_openai_agents_replay",
    "summarize_events",
    "to_json_text",
    "to_jsonable",
]
