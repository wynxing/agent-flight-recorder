"""FastAPI 应用。

本地单用户：无鉴权，只监听 localhost。这不是妥协，而是本地 MVP 的边界
（见 docs/replay-semantics.md 与 README 的"假设与边界"）。
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterator

from agent_flight_recorder.models import (
    EffectPolicy,
    IngestRequest,
    IngestResponse,
    RunRecord,
    ReplayPreset,
)
from agent_flight_recorder.otel import events_to_genai_spans
from agent_flight_recorder.recorder import doctor as sdk_doctor
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import background
from .agents import describe_agents, load_agent_specs
from .cases import create_case, snapshot_of, submit_case_run
from .config import get_settings
from .db import init_db, session_scope
from .diff import diff_runs
from .replay_runner import build_plan, estimate_replay, submit_replay
from .schemas import (
    AgentInfo,
    CaseCreateRequest,
    CaseItem,
    CaseListResponse,
    CaseRunRequest,
    CaseRunResponse,
    ReplayEstimateResponse,
    ReplayRequest,
    ReplayResponse,
    RunDetailResponse,
    RunListResponse,
    RunListItem,
    SuiteDetailResponse,
    SuiteEstimateResponse,
    SuiteListResponse,
    SuiteSubmitRequest,
    SuiteSubmitResponse,
    SuiteSummary,
    TimelineResponse,
    case_to_item,
    replay_meta_to_dict,
)
from .seed import seed_if_empty
from .suites import (
    SuiteRequestError,
    estimate_suite,
    submit_suite,
    suite_payload,
    suite_summaries,
)
from .storage import (
    get_case,
    get_events,
    get_run,
    ingest_batch,
    list_cases,
    list_children,
    list_runs,
    row_to_summary,
    run_to_record,
)
from .tables import CaseTable

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"succeeded", "failed", "aborted"}
STREAM_TIMEOUT_SECONDS = 180
STREAM_POLL_SECONDS = 0.35


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    init_db()
    settings = get_settings()
    logger.info("AFR server ready on http://%s:%s (db=%s)", settings.host, settings.port, settings.db_path)

    if settings.seed_on_startup:
        # 播种必须走登记在册的入口：它仍然是启动后异步进行、仍然不阻塞服务可用，
        # 但从此「还在播种」这件事能被等（见 background.py 与 issue #18）。
        background.start("afr-seed", _seed_quietly)
    yield


def _seed_quietly() -> None:
    try:
        seed_if_empty()
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed 流程异常：%s", exc)


app = FastAPI(
    title="Agent Flight Recorder",
    version="0.1.0",
    description="Replay, debug and evaluate AI agents like software.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ 健康与元信息


@app.get("/v1/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "ok": True,
        "service": "agent-flight-recorder",
        "version": "0.1.0",
        "db": str(settings.db_path),
        "seed_on_startup": settings.seed_on_startup,
    }


@app.get("/v1/doctor")
def doctor_endpoint() -> dict[str, Any]:
    """真实往返自检：证明录制链路是活的，而不是"配置文件存在"。"""

    settings = get_settings()
    return sdk_doctor(f"http://{settings.host}:{settings.port}").as_dict()


@app.get("/v1/agents", response_model=list[AgentInfo])
def list_agents() -> list[AgentInfo]:
    return [
        AgentInfo(
            name=item["name"],
            description=item.get("description", ""),
            version=item.get("version", ""),
            default_model=item.get("default_model"),
            default_system_prompt=item.get("default_system_prompt"),
            prompt_presets=item.get("prompt_presets", {}),
            tools=item.get("tools", []),
            can_replay=True,
            can_seed=True,
        )
        for item in describe_agents()
    ]


# ------------------------------------------------------------------ 入库


@app.post("/v1/ingest", response_model=IngestResponse)
def ingest(payload: IngestRequest) -> IngestResponse:
    try:
        with session_scope() as session:
            return ingest_batch(session, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ingest 失败：%s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------------------ Runs


@app.get("/v1/runs", response_model=RunListResponse)
def read_runs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    agent_name: str | None = None,
    status: str | None = None,
    parent_run_id: str | None = None,
    roots_only: bool = False,
) -> RunListResponse:
    with session_scope() as session:
        rows, total = list_runs(
            session,
            limit=limit,
            offset=offset,
            agent_name=agent_name,
            status=status,
            parent_run_id=parent_run_id,
            roots_only=roots_only,
        )
        items = [
            RunListItem(
                run=run_to_record(row),
                summary=row_to_summary(row),
                event_count=row.event_count,
                is_replay=row.parent_run_id is not None,
            )
            for row in rows
        ]
    return RunListResponse(runs=items, total=total)


@app.get("/v1/runs/{run_id}", response_model=RunDetailResponse)
def read_run(run_id: str) -> RunDetailResponse:
    with session_scope() as session:
        row = get_run(session, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        parent_row = get_run(session, row.parent_run_id) if row.parent_run_id else None
        children = list_children(session, run_id)
        registered = run_to_record(row).agent_name in load_agent_specs()

        return RunDetailResponse(
            run=run_to_record(row),
            summary=row_to_summary(row),
            event_count=row.event_count,
            parent=run_to_record(parent_row) if parent_row else None,
            children=[run_to_record(child) for child in children],
            replay=replay_meta_to_dict((row.meta or {}).get("afr_replay")),
            case=(row.meta or {}).get("afr_case"),
            agent_registered=registered,
        )


@app.get("/v1/runs/{run_id}/timeline", response_model=TimelineResponse)
def read_timeline(run_id: str, after_seq: int = Query(0, ge=0)) -> TimelineResponse:
    with session_scope() as session:
        row = get_run(session, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        return TimelineResponse(
            run=run_to_record(row),
            summary=row_to_summary(row),
            events=get_events(session, run_id, after_seq=after_seq),
        )


@app.get("/v1/runs/{run_id}/events/stream")
def stream_events(run_id: str, after_seq: int = Query(0, ge=0)) -> StreamingResponse:
    """SSE 实时尾随。让"正在跑的 Agent"和"正在回放的 Run"都能被看着发生。"""

    with session_scope() as session:
        if get_run(session, run_id) is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")

    return StreamingResponse(
        _event_stream(run_id, after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _event_stream(run_id: str, after_seq: int) -> Iterator[str]:
    cursor = after_seq
    deadline = time.monotonic() + STREAM_TIMEOUT_SECONDS

    while True:
        with session_scope() as session:
            row = get_run(session, run_id)
            status = row.status if row else "unknown"
            events = get_events(session, run_id, after_seq=cursor)

        for event in events:
            cursor = event.seq
            yield f"event: step{chr(10)}data: {event.model_dump_json()}{chr(10)}{chr(10)}"

        if not events and status in TERMINAL_STATUSES:
            yield f"event: done{chr(10)}data: {json.dumps({'status': status})}{chr(10)}{chr(10)}"
            return

        if time.monotonic() > deadline:
            yield f"event: timeout{chr(10)}data: {json.dumps({'status': status})}{chr(10)}{chr(10)}"
            return

        time.sleep(STREAM_POLL_SECONDS)


@app.get("/v1/runs/{run_id}/otel")
def export_otel(run_id: str) -> dict[str, Any]:
    """把录制事件映射成 OTel GenAI 语义约定（只做导出）。"""

    with session_scope() as session:
        row = get_run(session, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        run = run_to_record(row)
        events = get_events(session, run_id)
    return {"run_id": run_id, "spans": events_to_genai_spans(run, events)}


# ------------------------------------------------------------------ 回放


@app.post("/v1/runs/{run_id}/replay", response_model=ReplayResponse)
def start_replay(run_id: str, payload: ReplayRequest) -> ReplayResponse:
    _require_replayable(run_id, payload.from_seq)

    plan = build_plan(
        run_id,
        from_seq=payload.from_seq,
        preset=payload.preset,
        policy=payload.policy,
        budget=payload.budget,
        model=payload.model,
        system_prompt=payload.system_prompt,
        labels=payload.labels,
    )
    replay_run_id = submit_replay(plan)
    return ReplayResponse(
        run_id=replay_run_id,
        parent_run_id=run_id,
        from_seq=plan.from_seq,
        plan_summary=_plan_summary(plan),
    )


@app.post("/v1/runs/{run_id}/replay/estimate", response_model=ReplayEstimateResponse)
def estimate_replay_endpoint(run_id: str, payload: ReplayRequest) -> ReplayEstimateResponse:
    """预估这次回放大概要花多少：预计几次真实模型调用、大概多少成本。

    纯计算：不调用任何模型，也不创建 Run。父 Run 缺 token 或模型不在价格表内时，
    成本如实返回 null（无法预估），而不是给一个编出来的数字。
    """

    _require_replayable(run_id, payload.from_seq)
    plan = build_plan(
        run_id,
        from_seq=payload.from_seq,
        preset=payload.preset,
        policy=payload.policy,
        budget=payload.budget,
        model=payload.model,
        system_prompt=payload.system_prompt,
    )
    estimate = estimate_replay(plan)
    return ReplayEstimateResponse(**estimate.model_dump())


def _require_replayable(run_id: str, from_seq: int) -> None:
    """回放与预估共用的前置检查：起不来的回放，两个端点给同一个理由。"""

    with session_scope() as session:
        row = get_run(session, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        agent_name = row.agent_name
        if (row.meta or {}).get("runtime") == "pi":
            raise HTTPException(status_code=409, detail="pi 回放请使用 integrations/pi 本地运行器；服务端不执行 Node。")
        events = get_events(session, run_id)
        max_seq = max((event.seq for event in events), default=0)

    if from_seq > max_seq:
        raise HTTPException(
            status_code=400,
            detail=f"from_seq {from_seq} 超出该 Run 的最大步数 {max_seq}",
        )

    if agent_name not in load_agent_specs():
        raise HTTPException(
            status_code=409,
            detail=(
                f"agent {agent_name!r} 没有注册可重建的 Agent，无法在服务端发起回放。"
                "请安装对应的 Agent 包（entry point 组 afr.agents）。"
            ),
        )


def _plan_summary(plan) -> str:
    if plan.policy.by_kind.get("model_call") == "live" or (
        plan.policy.by_kind.get("model_call") is not None
        and getattr(plan.policy.by_kind.get("model_call"), "value", None) == "live"
    ):
        mode = "回归模式：模型真实执行，工具沿用录制结果"
    else:
        mode = "复现模式：全部使用录制结果，不产生真实调用"
    if plan.policy.allow_side_effect_execution:
        mode += "（已允许真实副作用）"
    return f"{mode}，从第 {plan.from_seq} 步开始"


# ------------------------------------------------------------------ Diff


@app.get("/v1/diff")
def read_diff(a: str, b: str) -> dict[str, Any]:
    with session_scope() as session:
        a_row = get_run(session, a)
        b_row = get_run(session, b)
        if a_row is None or b_row is None:
            missing = a if a_row is None else b
            raise HTTPException(status_code=404, detail=f"run not found: {missing}")
        a_run: RunRecord = run_to_record(a_row)
        b_run: RunRecord = run_to_record(b_row)
        a_events = get_events(session, a)
        b_events = get_events(session, b)
    return diff_runs(a_run, a_events, b_run, b_events).model_dump(mode="json")


# ------------------------------------------------------------------ Cases


@app.get("/v1/cases", response_model=CaseListResponse)
def read_cases(limit: int = Query(100, ge=1, le=500)) -> CaseListResponse:
    with session_scope() as session:
        rows = list_cases(session, limit=limit)
        items = [case_to_item(row, _source_run(session, row)) for row in rows]
    return CaseListResponse(cases=items)


def _source_run(session, row: CaseTable) -> RunRecord | None:
    source = get_run(session, row.source_run_id)
    return run_to_record(source) if source else None


@app.post("/v1/cases", response_model=CaseItem)
def create_case_endpoint(payload: CaseCreateRequest) -> CaseItem:
    with session_scope() as session:
        try:
            row = create_case(
                session,
                name=payload.name,
                source_run_id=payload.source_run_id,
                assertions=payload.assertions,
                description=payload.description,
                from_seq=payload.from_seq,
                to_seq=payload.to_seq,
                labels=payload.labels,
                preset=payload.preset,
                policy=payload.policy,
                model=payload.model,
                system_prompt=payload.system_prompt,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        source = get_run(session, payload.source_run_id)
        return case_to_item(row, run_to_record(source) if source else None)


@app.get("/v1/cases/{case_id}", response_model=CaseItem)
def read_case(case_id: str) -> CaseItem:
    with session_scope() as session:
        row = get_case(session, case_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"case not found: {case_id}")
        return case_to_item(row, _source_run(session, row))


@app.post("/v1/cases/{case_id}/run", response_model=CaseRunResponse)
def run_case(case_id: str, payload: CaseRunRequest) -> CaseRunResponse:
    try:
        run_id = submit_case_run(
            case_id,
            from_seq=payload.from_seq,
            preset=payload.preset,
            budget=payload.budget,
            model=payload.model,
            system_prompt=payload.system_prompt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CaseRunResponse(case_id=case_id, run_id=run_id)


# ------------------------------------------------------------------ 批量套件


@app.post("/v1/suites", response_model=SuiteSubmitResponse)
def submit_suite_endpoint(payload: SuiteSubmitRequest) -> SuiteSubmitResponse:
    """发起一次批量运行：一组用例 × 一组条件。

    立即返回批次标识，执行全部在后台，请求不阻塞；页面的进度来自对批次查询的轮询。
    """

    try:
        suite_id = submit_suite(
            case_ids=payload.case_ids,
            all_cases=payload.all_cases,
            conditions=[condition.model_dump() for condition in payload.conditions],
            budget=payload.budget,
        )
    except SuiteRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    with session_scope() as session:
        body = suite_payload(session, suite_id) or {}
    return SuiteSubmitResponse(
        suite_id=suite_id,
        status=str(body.get("status", "running")),
        total=int(body.get("total", 0)),
        conditions=payload.conditions,
    )


@app.post("/v1/suites/estimate", response_model=SuiteEstimateResponse)
def estimate_suite_endpoint(payload: SuiteSubmitRequest) -> SuiteEstimateResponse:
    """预估整批要花多少：一堆格子合起来预计几次真实模型调用、大概多少成本。

    只读：不调用任何模型，也不创建批次。父 Run 缺 token 记录、或模型不在本地价格表内时，
    成本如实返回 null（无法预估）并给出原因，而不是给一个编出来的数字——只要有一格给不出
    成本，整批就不报数字。
    """

    try:
        body = estimate_suite(
            case_ids=payload.case_ids,
            all_cases=payload.all_cases,
            conditions=[condition.model_dump() for condition in payload.conditions],
            budget=payload.budget,
        )
    except SuiteRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return SuiteEstimateResponse(**body)


@app.get("/v1/suites", response_model=SuiteListResponse)
def read_suites(limit: int = Query(20, ge=1, le=100)) -> SuiteListResponse:
    with session_scope() as session:
        summaries = suite_summaries(session, limit=limit)
    return SuiteListResponse(suites=[SuiteSummary(**item) for item in summaries])


@app.get("/v1/suites/{suite_id}", response_model=SuiteDetailResponse)
def read_suite(suite_id: str) -> SuiteDetailResponse:
    with session_scope() as session:
        body = suite_payload(session, suite_id)
    if body is None:
        raise HTTPException(status_code=404, detail=f"suite not found: {suite_id}")
    return SuiteDetailResponse(**body)


# ------------------------------------------------------------------ 静态控制台


def mount_console(application: FastAPI) -> bool:
    """如果控制台已经构建，就顺便托管它，这样单进程就能完成演示。"""

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if not dist.is_dir():
        return False
    application.mount("/", SpaStaticFiles(directory=str(dist), html=True), name="console")
    return True


class SpaStaticFiles(StaticFiles):
    """带 SPA 回退的静态托管。

    控制台用的是 HTML5 history 路由（/runs、/diff、/cases）。直接打开或刷新这些
    地址时磁盘上并没有对应文件，如果不回退到 index.html，用户看到的会是一段
    JSON 404 而不是界面。带扩展名的路径仍然按真实 404 处理，避免把缺失的静态
    资源伪装成成功响应。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not Path(path).suffix:
                return await super().get_response("index.html", scope)
            raise


mount_console(app)
