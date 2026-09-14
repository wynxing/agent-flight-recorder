"""回放引擎。

框架无关的核心在 engine.py；边界（失败收尾与成因恢复）在 boundary.py；
适配层按 runtime 取值解析：LangGraph 在 langgraph_adapter.py，
OpenAI Agents SDK 在 openai_agents_adapter.py（见 adapters.py）。
"""

from .adapters import (
    ADAPTER_MODULES,
    DEFAULT_ADAPTER,
    AdapterUnavailableError,
    UnknownAdapterError,
    adapter_names,
    load_adapter,
)
from .boundary import apply_plan_to_recorder, cause_of, finish_failed_replay
from .budget import (
    STOPPED_BY_COST,
    STOPPED_BY_MODEL_CALLS,
    BudgetLedger,
    BudgetUsage,
    ReplayEstimate,
    estimate_replay_budget,
    usage_detail,
)
from .effects import (
    RecordedEffects,
    RecordedModelResponse,
    RecordedToolResult,
    args_key,
)
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
    "ADAPTER_MODULES",
    "CAUSE_CODES",
    "CAUSE_PRECEDENCE",
    "DEFAULT_ADAPTER",
    "STOPPED_BY_COST",
    "STOPPED_BY_MODEL_CALLS",
    "AdapterUnavailableError",
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
    "UnknownAdapterError",
    "adapter_names",
    "args_key",
    "apply_plan_to_recorder",
    "behavioral_steps",
    "cause_of",
    "classify_step",
    "detect_fork",
    "estimate_replay_budget",
    "finish_failed_replay",
    "load_adapter",
    "most_significant",
    "plan_summary",
    "usage_detail",
]

