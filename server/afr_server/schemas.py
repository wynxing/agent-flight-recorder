"""API 请求与响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_flight_recorder.models import EffectPolicy, Event, ReplayPreset, RunRecord, RunSummary
from pydantic import BaseModel, Field

from .assertions import AssertionResult, AssertionSpec
from .diff import RunDiff
from .tables import CaseTable


class RunListItem(BaseModel):
    run: RunRecord
    summary: RunSummary
    event_count: int
    is_replay: bool


class RunListResponse(BaseModel):
    runs: list[RunListItem]
    total: int


class RunDetailResponse(BaseModel):
    run: RunRecord
    summary: RunSummary
    event_count: int
    parent: RunRecord | None = None
    children: list[RunRecord] = Field(default_factory=list)
    replay: dict[str, Any] | None = None
    case: dict[str, Any] | None = None
    agent_registered: bool = False


class TimelineResponse(BaseModel):
    run: RunRecord
    summary: RunSummary
    events: list[Event]


class ReplayRequest(BaseModel):
    from_seq: int = Field(ge=1)
    preset: ReplayPreset | None = None
    policy: EffectPolicy | None = None
    model: str | None = None
    system_prompt: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class ReplayResponse(BaseModel):
    run_id: str
    parent_run_id: str
    from_seq: int
    plan_summary: str
    status: str = "running"


class CaseCreateRequest(BaseModel):
    name: str
    description: str = ""
    source_run_id: str
    from_seq: int | None = None
    to_seq: int | None = None
    assertions: list[AssertionSpec] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    preset: ReplayPreset | None = None
    policy: EffectPolicy | None = None
    model: str | None = None


class CaseItem(BaseModel):
    id: str
    name: str
    description: str
    source_run_id: str
    from_seq: int | None = None
    to_seq: int | None = None
    assertions: list[AssertionSpec] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    preset: ReplayPreset | None = None
    policy: EffectPolicy | None = None
    model: str | None = None
    last_status: str | None = None
    last_run_id: str | None = None
    last_run_at: datetime | None = None
    last_results: list[AssertionResult] = Field(default_factory=list)
    created_at: datetime | None = None
    source_run: RunRecord | None = None


class CaseListResponse(BaseModel):
    cases: list[CaseItem]


class CaseRunRequest(BaseModel):
    from_seq: int | None = None
    preset: ReplayPreset | None = None
    model: str | None = None
    system_prompt: str | None = None


class CaseRunResponse(BaseModel):
    case_id: str
    run_id: str
    status: str = "running"


class AgentInfo(BaseModel):
    name: str
    description: str = ""
    version: str = ""
    default_model: str | None = None
    tools: list[str] = Field(default_factory=list)
    can_replay: bool = True
    can_seed: bool = False


def case_to_item(row: CaseTable, source_run: RunRecord | None = None) -> CaseItem:
    policy = row.effect_policy or {}
    return CaseItem(
        id=row.id,
        name=row.name,
        description=row.description,
        source_run_id=row.source_run_id,
        from_seq=row.from_seq,
        to_seq=row.to_seq,
        assertions=[AssertionSpec.model_validate(a) for a in (row.assertions or [])],
        labels=dict(row.labels or {}),
        preset=_preset(policy.get("preset")),
        policy=EffectPolicy.model_validate(policy["policy"]) if policy.get("policy") else None,
        model=policy.get("model"),
        last_status=row.last_status,
        last_run_id=row.last_run_id,
        last_run_at=row.last_run_at,
        last_results=[AssertionResult.model_validate(item) for item in (row.last_results or [])],
        created_at=row.created_at,
        source_run=source_run,
    )


def _preset(value: Any) -> ReplayPreset | None:
    if value is None:
        return None
    try:
        return ReplayPreset(str(value))
    except ValueError:
        return None


__all__ = [
    "AgentInfo",
    "CaseCreateRequest",
    "CaseItem",
    "CaseListResponse",
    "CaseRunRequest",
    "CaseRunResponse",
    "ReplayRequest",
    "ReplayResponse",
    "RunDetailResponse",
    "RunDiff",
    "RunListItem",
    "RunListResponse",
    "TimelineResponse",
    "case_to_item",
]

