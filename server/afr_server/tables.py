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
    #: 最近一次执行用的条件（prompt 版本 / 模型）。缺了它，用例页上那句「最近一次结论」
    #: 就没有前提：同一批里两个 Prompt 版本都能写进这一列，谁也说不清看到的是哪一次。
    last_condition: Any = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))


class SuiteTable(SQLModel, table=True):
    """一次批量运行：一组用例 × 一组条件。

    这张表只记本次批量的请求（用例集合与条件集合）与生命周期，每个格子的结论落在
    SuiteItemTable 上。条件刻意存在两处而不是只存一份：套件层保留用户请求的顺序
    （界面按它分组呈现），格子层保留这次执行实际用的条件（结论永远带着前提）。
    """

    __tablename__ = "suites"

    id: str = Field(primary_key=True)
    #: running / finished。落库值只是当前值；查询时以格子的实际状态重新推导，
    #: 因此进程在批量中途重启，也不会留下一个永远「进行中」的批次。
    status: str = Field(default="running", index=True)
    case_ids: Any = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    conditions: Any = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
    finished_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime, nullable=True))


class SuiteItemTable(SQLModel, table=True):
    """套件里的一格：某条用例在某个条件下的执行结果。

    condition 与结论一起落库。缺了它，「这条用例的结论」就没有前提：同一批里两个
    Prompt 版本的格子会长得一模一样，界面也无从分辨哪条属于哪个条件。
    """

    __tablename__ = "suite_items"
    __table_args__ = (
        # 一个套件里「同一用例 × 同一条件」只能有一个格子。
        UniqueConstraint("suite_id", "case_id", "condition_key", name="uq_suite_items_cell"),
        Index("ix_suite_items_suite_case", "suite_id", "case_id"),
    )

    id: str = Field(primary_key=True)
    suite_id: str = Field(index=True)
    case_id: str = Field(index=True)
    case_name: str = ""
    #: 在本次批量里的落位，保住「用户选的顺序」，界面不必自己重排。
    position: int = Field(default=0)
    #: 条件的规范化键（由 prompt / model 名字构成），聚合按它分组。
    condition_key: str = Field(index=True)
    #: 这次执行实际用的条件：prompt 版本名、模型，以及解析出来的 Prompt 正文。
    condition: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    #: pending / running / passed / failed / inconclusive / error。
    status: str = Field(default="pending", index=True)
    run_id: Optional[str] = None
    results: Any = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    cause: Any = Field(default=None, sa_column=Column(JSON, nullable=True))
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime, nullable=True))
    ended_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime, nullable=True))

