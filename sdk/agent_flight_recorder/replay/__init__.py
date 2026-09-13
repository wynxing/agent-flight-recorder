"""回放引擎。

框架无关的核心在 engine.py；LangGraph 适配层在 langgraph_adapter.py。
"""

from .effects import RecordedEffects, RecordedModelResponse, RecordedToolResult, args_key
from .engine import (
    IncompleteReplay,
    ReplayDivergence,
    ReplayExhaustedError,
    ReplayOverrides,
    ReplayPlan,
    ReplayResult,
    ReplaySession,
    SideEffectBlocked,
    StepPlan,
    plan_summary,
)
from .fork import Fork, ForkKind, behavioral_steps, classify_step, detect_fork
from .reasons import (
    CAUSE_CODES,
    CAUSE_PRECEDENCE,
    InconclusiveCode,
    InconclusiveReason,
    most_significant,
)

__all__ = [
    "CAUSE_CODES",
    "CAUSE_PRECEDENCE",
    "Fork",
    "ForkKind",
    "IncompleteReplay",
    "InconclusiveCode",
    "InconclusiveReason",
    "RecordedEffects",
    "RecordedModelResponse",
    "RecordedToolResult",
    "ReplayExhaustedError",
    "ReplayDivergence",
    "ReplayOverrides",
    "ReplayPlan",
    "ReplayResult",
    "ReplaySession",
    "SideEffectBlocked",
    "StepPlan",
    "args_key",
    "behavioral_steps",
    "classify_step",
    "detect_fork",
    "most_significant",
    "plan_summary",
]

