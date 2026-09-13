"""持久化表结构。

事件表刻意保持 append-only：没有任何更新路径，只有插入与（去重用的）存在性检查。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Column, DateTime, Index, JSON, UniqueConstraint
from sqlmodel import Field, SQLModel

from agent_flight_recorder.models import utcnow


class RunTable(SQLModel, table=True):
    __tablename__ = "runs"

    id: str = Field(primary_key=True)
    agent_name: str = Field(index=True)
    agent_version: Optional[str] = None
    model: Optional[str] = None
    status: str = Field(default="running", index=True)
    parent_run_id: Optional[str] = Field(default=None, index=True)
    replay_from_seq: Optional[int] = None
    effect_policy: Any = Field(default=None, sa_column=Column(JSON, nullable=True))
    prompt_version: Optional[str] = None
    labels: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    meta: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    redactions: Any = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    summary: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    event_count: int = Field(default=0)
    started_at: datetime = Field(sa_column=Column(DateTime, nullable=False, index=True))
    ended_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))


class EventTable(SQLModel, table=True):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_events_run_seq"),
        Index("ix_events_run_seq", "run_id", "seq"),
    )

    id: str = Field(primary_key=True)
    run_id: str = Field(index=True)
    seq: int = Field(index=True)
    type: str = Field(index=True)
    parent_seq: Optional[int] = None
    source_seq: Optional[int] = None
    name: Optional[str] = Field(default=None, index=True)
    started_at: datetime = Field(sa_column=Column(DateTime, nullable=False))
    ended_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime, nullable=True))
    duration_ms: Optional[float] = None
    input: Any = Field(default=None, sa_column=Column(JSON, nullable=True))
    output: Any = Field(default=None, sa_column=Column(JSON, nullable=True))
    error: Any = Field(default=None, sa_column=Column(JSON, nullable=True))
    tokens: Any = Field(default=None, sa_column=Column(JSON, nullable=True))
    cost_usd: Optional[float] = None
    side_effect: Optional[str] = Field(default=None, index=True)
    effect_source: Optional[str] = Field(default=None, index=True)
    redactions: Any = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    attributes: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class CaseTable(SQLModel, table=True):
    """把生产失败沉淀成可重复执行的测试用例。

    断言与条件标签是一等字段：缺了它们，Eval Dataset 就只是一堆噪音。
    """

    __tablename__ = "cases"

    id: str = Field(primary_key=True)
    name: str = Field(index=True)
    description: str = ""
    source_run_id: str = Field(index=True)
    from_seq: Optional[int] = None
    to_seq: Optional[int] = None
    assertions: Any = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    labels: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    effect_policy: Any = Field(default=None, sa_column=Column(JSON, nullable=True))
    last_status: Optional[str] = Field(default=None, index=True)
    last_run_id: Optional[str] = None
    last_run_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime, nullable=True))
    last_results: Any = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # 结论的成因（结构化的 {code, detail}）。只有 last_status 没有成因的用例，
    # 用户无从知道该去看录制质量还是副作用策略。
    last_cause: Any = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))

