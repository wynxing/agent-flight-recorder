"""存储层：入库、脱敏、查询。

脱敏在入库时执行，因此 SDK 本地缓冲里仍是明文；生产部署应把 SDK 的 endpoint
视为可信边界（见 docs/protocol.md 第 5 节）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from agent_flight_recorder.models import (
    Event,
    EventType,
    IngestRequest,
    IngestResponse,
    RunRecord,
    RunStatus,
    RunSummary,
    summarize_events,
    utcnow,
)
from agent_flight_recorder.redact import redact
from sqlmodel import Session, col, select

from .tables import (
    CaseSetVersionTable,
    CaseTable,
    EventTable,
    RunTable,
    SuiteItemTable,
    SuiteTable,
)


def naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ingest_batch(session: Session, payload: IngestRequest) -> IngestResponse:
    run_payload = payload.run.model_dump(mode="python")
    run_payload, run_hits = _redact_fields(run_payload, ("metadata", "labels"))

    row = session.get(RunTable, payload.run.id)
    if row is None:
        row = RunTable(
            id=payload.run.id,
            agent_name=payload.run.agent_name,
            started_at=naive_utc(payload.run.started_at) or utcnow(),
        )
        session.add(row)

    row.agent_name = payload.run.agent_name
    row.agent_version = payload.run.agent_version
    row.model = payload.run.model
    row.status = payload.run.status.value
    row.parent_run_id = payload.run.parent_run_id
    row.replay_from_seq = payload.run.replay_from_seq
    row.effect_policy = (
        payload.run.effect_policy.model_dump(mode="json") if payload.run.effect_policy else None
    )
    row.prompt_version = payload.run.prompt_version
    row.labels = run_payload.get("labels") or {}
    row.meta = run_payload.get("metadata") or {}
    row.redactions = sorted(set(row.redactions or []) | set(run_hits))
    row.started_at = naive_utc(payload.run.started_at) or row.started_at
    row.ended_at = naive_utc(payload.run.ended_at)
    row.updated_at = utcnow()

    accepted = 0
    duplicates = 0
    rejected: list[dict[str, Any]] = []
    existing = _existing_seqs(session, payload.run.id)
    seen_in_batch: set[int] = set()

    for event in payload.events:
        if event.seq in existing or event.seq in seen_in_batch:
            duplicates += 1
            continue
        if event.run_id != payload.run.id:
            rejected.append({"seq": event.seq, "reason": "run_id mismatch"})
            continue
        try:
            session.add(_event_row(_clean_event(event)))
            seen_in_batch.add(event.seq)
            accepted += 1
        except Exception as exc:  # noqa: BLE001 - 单条坏事件不能拖垮整批
            rejected.append({"seq": event.seq, "reason": f"{type(exc).__name__}: {exc}"})

    session.flush()

    events = get_events(session, payload.run.id)
    summary = summarize_events(events)
    row.summary = summary.model_dump(mode="json")
    row.event_count = len(events)
    row.updated_at = utcnow()
    session.add(row)
    session.flush()

    return IngestResponse(
        run_id=payload.run.id,
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
        max_seq=max((e.seq for e in events), default=0),
    )


def _existing_seqs(session: Session, run_id: str) -> set[int]:
    statement = select(EventTable.seq).where(EventTable.run_id == run_id)
    return set(session.exec(statement).all())


def _clean_event(event: Event) -> Event:
    data = event.model_dump(mode="python")
    data, hits = _redact_fields(data, ("input", "output", "attributes"))
    if data.get("error"):
        data["error"], error_hits = redact(data["error"])
        hits.extend(error_hits)
    if hits:
        data["redactions"] = sorted(set(data.get("redactions") or []) | set(hits))
    return Event.model_validate(data)


def _redact_fields(data: dict[str, Any], fields: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    hits: list[str] = []
    for field in fields:
        if data.get(field) is None:
            continue
        data[field], found = redact(data[field])
        hits.extend(found)
    return data, hits


def _event_row(event: Event) -> EventTable:
    return EventTable(
        id=event.id,
        run_id=event.run_id,
        seq=event.seq,
        type=event.type.value,
        parent_seq=event.parent_seq,
        source_seq=event.source_seq,
        name=event.name,
        started_at=naive_utc(event.started_at) or utcnow(),
        ended_at=naive_utc(event.ended_at),
        duration_ms=event.duration_ms,
        input=_json(event.input),
        output=_json(event.output),
        error=_json(event.error.model_dump(mode="json") if event.error else None),
        tokens=_json(event.tokens.model_dump(mode="json") if event.tokens else None),
        cost_usd=event.cost_usd,
        side_effect=event.side_effect.value if event.side_effect else None,
        effect_source=event.effect_source.value if event.effect_source else None,
        redactions=list(event.redactions),
        attributes=_json(event.attributes) or {},
    )


def _json(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, ensure_ascii=False, default=str)) if value is not None else None


def get_run(session: Session, run_id: str) -> RunTable | None:
    return session.get(RunTable, run_id)


def list_runs(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    agent_name: str | None = None,
    status: str | None = None,
    parent_run_id: str | None = None,
    roots_only: bool = False,
) -> tuple[list[RunTable], int]:
    statement = select(RunTable)
    count_statement = select(RunTable)
    filters = []
    if agent_name:
        filters.append(RunTable.agent_name == agent_name)
    if status:
        filters.append(RunTable.status == status)
    if parent_run_id:
        filters.append(RunTable.parent_run_id == parent_run_id)
    elif roots_only:
        filters.append(col(RunTable.parent_run_id).is_(None))

    for condition in filters:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    statement = statement.order_by(col(RunTable.started_at).desc()).offset(offset).limit(limit)
    rows = list(session.exec(statement).all())
    total = len(session.exec(count_statement).all())
    return rows, total


def list_children(session: Session, run_id: str) -> list[RunTable]:
    statement = (
        select(RunTable)
        .where(RunTable.parent_run_id == run_id)
        .order_by(col(RunTable.started_at).asc())
    )
    return list(session.exec(statement).all())


def get_events(session: Session, run_id: str, *, after_seq: int = 0) -> list[Event]:
    statement = (
        select(EventTable)
        .where(EventTable.run_id == run_id)
        .where(EventTable.seq > after_seq)
        .order_by(col(EventTable.seq).asc())
    )
    return [_row_to_event(row) for row in session.exec(statement).all()]


def run_to_record(row: RunTable) -> RunRecord:
    from agent_flight_recorder.models import EffectPolicy

    return RunRecord(
        id=row.id,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        model=row.model,
        status=RunStatus(row.status),
        parent_run_id=row.parent_run_id,
        replay_from_seq=row.replay_from_seq,
        effect_policy=EffectPolicy.model_validate(row.effect_policy) if row.effect_policy else None,
        prompt_version=row.prompt_version,
        labels=row.labels or {},
        metadata=row.meta or {},
        started_at=aware_utc(row.started_at),
        ended_at=aware_utc(row.ended_at),
        redactions=list(row.redactions or []),
    )


def row_to_summary(row: RunTable) -> RunSummary:
    if row.summary:
        return RunSummary.model_validate(row.summary)
    return RunSummary(event_count=row.event_count)


def _row_to_event(row: EventTable) -> Event:
    from agent_flight_recorder.models import EffectSource, ErrorInfo, SideEffect, TokenUsage

    return Event(
        id=row.id,
        run_id=row.run_id,
        seq=row.seq,
        type=EventType(row.type),
        parent_seq=row.parent_seq,
        source_seq=row.source_seq,
        name=row.name,
        started_at=aware_utc(row.started_at),
        ended_at=aware_utc(row.ended_at),
        duration_ms=row.duration_ms,
        input=row.input,
        output=row.output,
        error=ErrorInfo.model_validate(row.error) if row.error else None,
        tokens=TokenUsage.model_validate(row.tokens) if row.tokens else None,
        cost_usd=row.cost_usd,
        side_effect=SideEffect(row.side_effect) if row.side_effect else None,
        effect_source=EffectSource(row.effect_source) if row.effect_source else None,
        redactions=list(row.redactions or []),
        attributes=row.attributes or {},
    )


def final_output_of(events: Sequence[Event]) -> str | None:
    ordered = list(events)
    for event in reversed(ordered):
        if event.type is EventType.RUN_FINISHED:
            result = (event.output or {}).get("result")
            return result if isinstance(result, str) else (str(result) if result is not None else None)
    for event in reversed(ordered):
        if event.type is EventType.MODEL_CALL:
            text = (event.output or {}).get("text")
            if isinstance(text, str) and text.strip():
                return text
    return None


def list_cases(session: Session, *, limit: int = 100) -> list[CaseTable]:
    statement = select(CaseTable).order_by(col(CaseTable.created_at).desc()).limit(limit)
    return list(session.exec(statement).all())


def get_case(session: Session, case_id: str) -> CaseTable | None:
    return session.get(CaseTable, case_id)


def get_case_set_version(session: Session, version_id: str) -> CaseSetVersionTable | None:
    """按标识取回一个用例集版本（内容决定的可寻址标识，见 case_versions.py）。"""

    return session.get(CaseSetVersionTable, version_id)


def get_suite(session: Session, suite_id: str) -> SuiteTable | None:
    return session.get(SuiteTable, suite_id)


def list_suites(session: Session, *, limit: int = 20) -> list[SuiteTable]:
    statement = select(SuiteTable).order_by(col(SuiteTable.created_at).desc()).limit(limit)
    return list(session.exec(statement).all())


def list_suite_items(session: Session, suite_id: str) -> list[SuiteItemTable]:
    """按提交时的落位返回格子。

    顺序稳定是有意的：界面按条件分组呈现，格子在自己那一列里的先后必须与用户挑
    用例的顺序一致，否则同一个批量的两次刷新看起来会像两批不同的结果。
    """

    statement = (
        select(SuiteItemTable)
        .where(SuiteItemTable.suite_id == suite_id)
        .order_by(col(SuiteItemTable.position).asc())
    )
    return list(session.exec(statement).all())


def update_run_metadata(session: Session, run_id: str, patch: dict[str, Any]) -> None:
    """合并式更新 Run 的 metadata。回放结果、Case 归属等都走这里。"""

    row = session.get(RunTable, run_id)
    if row is None:
        return
    merged = dict(row.meta or {})
    merged.update(patch)
    row.meta = merged
    row.updated_at = utcnow()
    session.add(row)
    session.flush()
