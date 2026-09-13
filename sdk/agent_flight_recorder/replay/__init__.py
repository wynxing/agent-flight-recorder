"""回放引擎。

框架无关的核心在 engine.py；LangGraph 适配层在 langgraph_adapter.py。
"""

from .budget import (
    STOPPED_BY_COST,
    STOPPED_BY_MODEL_CALLS,
    BudgetLedger,
    BudgetUsage,
    ReplayEstimate,
    estimate_replay_budget,
    usage_detail,
)
from .effects import RecordedEffects, RecordedModelResponse, RecordedToolResult, args_key
from .engine import (
    ReplayExhaustedError,
    ReplayOverrides,
    ReplayPlan,
    ReplayResult,
    ReplaySession,
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
    "STOPPED_BY_COST",
    "STOPPED_BY_MODEL_CALLS",
    "BudgetLedger",
    "BudgetUsage",
    "Fork",
    "ForkKind",
    "InconclusiveCode",
    "InconclusiveReason",
    "RecordedEffects",
    "RecordedModelResponse",
    "RecordedToolResult",
    "ReplayExhaustedError",
    "ReplayEstimate",
    "ReplayOverrides",
    "ReplayPlan",
    "ReplayResult",
    "ReplaySession",
    "StepPlan",
    "args_key",
    "behavioral_steps",
    "classify_step",
    "detect_fork",
    "estimate_replay_budget",
    "most_significant",
    "plan_summary",
    "usage_detail",
]

