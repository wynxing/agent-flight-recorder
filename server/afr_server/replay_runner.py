"""回放调度。

本地 MVP 中由服务端作为宿主执行回放（见 docs/replay-semantics.md 第 6 节）。
回放引擎本身位于 SDK 内，真实用户完全可以脱离平台在自己的进程里跑。
"""

from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent_flight_recorder.models import (
    EffectPolicy,
    ReplayBudget,
    ReplayPreset,
    RunRecord,
    RunStatus,
    new_id,
    utcnow,
)
from agent_flight_recorder.recorder import Recorder
from agent_flight_recorder.replay.budget import ReplayEstimate, estimate_replay_budget
from agent_flight_recorder.replay.engine import ReplayPlan, ReplaySession
from agent_flight_recorder.replay.engine import ReplayExhaustedError, ensure_replay_context
from agent_flight_recorder.replay.langgraph_adapter import apply_plan_to_recorder, run_replay
from agent_flight_recorder.replay.reasons import InconclusiveCode, InconclusiveReason

from .agents import resolve_agent_spec
from .db import session_scope
from .storage import get_events, get_run, naive_utc, run_to_record, update_run_metadata
from .tables import RunTable
from .transport import DirectTransport

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="afr-replay")


def build_plan(
    parent_run_id: str,
    *,
    from_seq: int,
    preset: ReplayPreset | None = None,
    policy: EffectPolicy | None = None,
    budget: ReplayBudget | None = None,
    overrides: dict[str, Any] | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    labels: dict[str, str] | None = None,
) -> ReplayPlan:
    resolved = policy or (
        EffectPolicy.regress() if preset is ReplayPreset.REGRESS else EffectPolicy.reproduce()
    )
    overrides_payload = dict(overrides or {})
    if model is not None:
        overrides_payload["model"] = model
    if system_prompt is not None:
        overrides_payload["system_prompt"] = system_prompt

    return ReplayPlan(
        parent_run_id=parent_run_id,
        from_seq=from_seq,
        policy=resolved,
        budget=budget or ReplayBudget(),
        overrides=overrides_payload,
        labels=dict(labels or {}),
    )


def submit_replay(plan: ReplayPlan, *, run_id: str | None = None) -> str:
    """提交一次回放，立即返回新的 run_id。真正的执行在后台线程。"""

    replay_run_id = run_id or new_id()
    # 提交时就先做一次录制质量预检：能立刻确定的成因（例如录制本身不足以支撑回放）
    # 要在回放 Run 出现的那一刻就写在它身上，否则用户会先看到一段没有成因的等待期。
    prepare_run(plan, replay_run_id, cause=preflight_cause(plan))
    _executor.submit(_run_job, plan, replay_run_id)
    return replay_run_id


def preflight_cause(plan: ReplayPlan) -> InconclusiveReason | None:
    """不产生任何副作用的录制质量预检，返回成因或 None。

    与真正的执行共用同一条判定路径（``ReplaySession.validate_recording`` 与
    ``ensure_replay_context``），因此预检不会与执行结果说两套话。
    """

    try:
        with session_scope() as session:
            parent_row = get_run(session, plan.parent_run_id)
            if parent_row is None:
                return None
            parent_run: RunRecord = run_to_record(parent_row)
            parent_events = get_events(session, plan.parent_run_id)

        session = ReplaySession(plan, parent_run, parent_events)
        session.validate_recording()
        ensure_replay_context(
            initial_state=None,
            parent_metadata=parent_run.metadata,
            initial_input=session.initial_input,
        )
    except ReplayExhaustedError as exc:
        return exc.cause
    return None

def estimate_replay(plan: ReplayPlan) -> ReplayEstimate:
    """预估一次回放要花多少。纯计算：不调用任何模型，也不创建 Run。

    与真正的执行共用同一套策略解析与同一份录制，因此预估与实跑不会说两套话：
    分叉点之前的步骤一律按录制结果复现，只有解析成 live 的模型调用才计入。
    """

    with session_scope() as session:
        parent_row = get_run(session, plan.parent_run_id)
        if parent_row is None:
            raise ValueError(f"parent run not found: {plan.parent_run_id}")
        parent_run: RunRecord = run_to_record(parent_row)
        parent_events = get_events(session, plan.parent_run_id)
    return estimate_replay_budget(plan, parent_run, parent_events)


def prepare_run(
    plan: ReplayPlan,
    replay_run_id: str,
    *,
    labels: dict[str, str] | None = None,
    case: dict[str, Any] | None = None,
    cause: InconclusiveReason | None = None,
) -> None:
    """在后台执行之前就把 Run 行建好。

    否则从"发起回放"到"第一批事件入库"之间有一段窗口期，用户点开刚拿到的链接会看到
    404。回放是一个立刻可见的对象，不能等第一批事件到了才存在。
    """

    with session_scope() as session:
        parent_row = get_run(session, plan.parent_run_id)
        if parent_row is None:
            raise ValueError(f"parent run not found: {plan.parent_run_id}")

        meta: dict[str, Any] = {
            "afr_replay": {
                "parent_run_id": plan.parent_run_id,
                "from_seq": plan.from_seq,
                "policy": plan.policy.model_dump(mode="json"),
                "pending": True,
                "complete": True,
                "reason": None,
                "cause": None,
            }
        }
        if cause is not None:
            # 并列成因在提交时就已经定了（例如录制本身不足以支撑回放）：
            # 控制台不该在等待期间看到「没有成因」。
            meta["afr_replay"].update(
                complete=False, reason=cause.model_dump(mode="json"), cause=cause.model_dump(mode="json")
            )
        if case is not None:
            meta["afr_case"] = case

        session.add(
            RunTable(
                id=replay_run_id,
                agent_name=parent_row.agent_name,
                agent_version=parent_row.agent_version,
                model=plan.overrides.model or parent_row.model,
                status=RunStatus.RUNNING.value,
                parent_run_id=plan.parent_run_id,
                replay_from_seq=plan.from_seq,
                effect_policy=plan.policy.model_dump(mode="json"),
                prompt_version=parent_row.prompt_version,
                labels={**(parent_row.labels or {}), **(labels or {})},
                meta=meta,
                started_at=naive_utc(utcnow()),
            )
        )


def _run_job(plan: ReplayPlan, replay_run_id: str) -> None:
    try:
        execute_replay(plan, replay_run_id)
    except Exception as exc:  # noqa: BLE001 - 后台任务必须吞掉异常并留下痕迹
        logger.warning("replay job failed: %s", exc)
        _record_failure(plan, replay_run_id, exc)


def execute_replay(plan: ReplayPlan, replay_run_id: str):
    """同步执行一次回放并返回 ReplayResult。失败时抛异常。"""

    with session_scope() as session:
        parent_row = get_run(session, plan.parent_run_id)
        if parent_row is None:
            raise ValueError(f"parent run not found: {plan.parent_run_id}")
        parent_run: RunRecord = run_to_record(parent_row)
        parent_events = get_events(session, plan.parent_run_id)

    spec = resolve_agent_spec(parent_run.agent_name)
    if spec is None:
        raise RuntimeError(
            f"没有为 agent {parent_run.agent_name!r} 注册可重建的 Agent。"
            "回放需要在服务端进程中构建该 Agent，请安装对应的 Agent 包。"
        )

    recorder = Recorder(
        parent_run.agent_name,
        agent_version=parent_run.agent_version,
        model=plan.overrides.model or parent_run.model,
        prompt_version=parent_run.prompt_version,
        labels={**parent_run.labels, **plan.labels},
        run_id=replay_run_id,
        parent_run_id=plan.parent_run_id,
        replay_from_seq=plan.from_seq,
        effect_policy=plan.policy,
        transport=DirectTransport(),
        flush_interval=0.5,
    )
    apply_plan_to_recorder(plan, recorder)

    session = ReplaySession(plan, parent_run, parent_events)
    try:
        result = run_replay(
            session=session,
            recorder=recorder,
            agent_factory=spec.build,
            tool_side_effects=spec.tool_side_effects,
        )
    finally:
        recorder.close()

    if recorder.stats.events_dropped or recorder.stats.batches_failed:
        # 就近构造成因：以前这里自造了 ``replay_recording_loss``，与 SDK 的
        # ``recording_loss`` 是同一件事的第三个名字。现在用共享分类。
        result.complete = False
        result.reason = InconclusiveReason.from_code(
            InconclusiveCode.RECORDING_LOSS.value,
            f"服务端记录器丢事件（dropped={recorder.stats.events_dropped}, "
            f"batches_failed={recorder.stats.batches_failed}）",
        )

    replay_meta: dict[str, Any] = {
        "parent_run_id": plan.parent_run_id,
        "from_seq": plan.from_seq,
        "policy": plan.policy.model_dump(mode="json"),
        "first_fork": result.first_fork.model_dump(mode="json") if result.first_fork else None,
        "forks": [fork.model_dump(mode="json") for fork in result.forks],
        "verdict": result.status,
        "complete": result.complete,
        # 新增结构化成因；旧字段保持可读（结构与文本形态都能被解析回成因）。
        "reason": result.reason.model_dump(mode="json") if result.reason else None,
        "cause": result.reason.model_dump(mode="json") if result.reason else None,
    }
    if result.budget is not None:
        # 记账只出现在声明了上限的回放上：没声明过上限的回放不该凭空多出一个
        # 「上限：无」的账目，因此不设上限的行为与加这套能力之前逐字一致。
        replay_meta["budget"] = result.budget.model_dump(mode="json")
    with session_scope() as db:
        update_run_metadata(db, replay_run_id, {"afr_replay": replay_meta})
    return result


def _record_failure(plan: ReplayPlan, replay_run_id: str, exc: BaseException) -> None:
    """即使回放起不来，也要留下一个可诊断的 Run，而不是让 UI 空等。"""

    try:
        recorder = Recorder(
            "afr-replay",
            run_id=replay_run_id,
            parent_run_id=plan.parent_run_id,
            replay_from_seq=plan.from_seq,
            effect_policy=plan.policy,
            transport=DirectTransport(),
            flush_interval=0.0,
        )
        recorder.start(task=f"replay from step {plan.from_seq}")
        recorder.record_error(
            exc,
            **{"stack": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))},
        )
        recorder.finish(status=RunStatus.FAILED)
        recorder.close(drain_timeout=2.0)
    except Exception as inner:  # noqa: BLE001
        logger.warning("failed to record replay failure: %s", inner)
