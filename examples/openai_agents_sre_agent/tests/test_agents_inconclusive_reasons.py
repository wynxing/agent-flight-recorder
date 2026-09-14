"""端到端：第二个框架上，「拿不到结论」的成因分类同样成立。

与 examples/langgraph_sre_agent/tests/test_inconclusive_reasons.py 对称的部分：
副作用被闸门拦下与录制丢失各自给出不同的码，用例判定都落在 inconclusive。

本文件还有两条**第二个框架特有的**守卫，它们钉的是本轮真的修掉的东西：

* 工具步骤上「没有匹配的录制结果」必须变成一条带码的结论，而不是一次看似成功的运行
  ——Agents SDK 默认会把工具异常变成给模型看的文本，那样回放会继续跑完；
* 预算在工具步骤上触顶时同样要保留 budget_exceeded——异常是被框架包过一层之后
  才到边界的，成因必须从异常链里恢复出来。

它们由三层共同保证，且**任何单独一层都不是唯一的承重点**（这正是刻意的纵深）：
引擎在步边界把判定记在会话上（`ReplaySession.incomplete_reason`）→ 适配层在 Runner
返回后再检查一次 → 边界层从异常链或会话状态恢复成因。

实测（见本轮 PR 的反证表）：单独关掉异常链恢复（`_cause_in_chain`）或单独关掉会话兜底，
这两条仍然是绿的——另一条会把成因接住；把两个恢复环节同时关掉，它们才变红。
因此每一层的**单元级**守卫放在 sdk/tests/test_replay_adapters.py 里（关掉链恢复 2 红、
关掉会话兜底 1 红），本文件守的是端到端可观察的结论。
"""

from __future__ import annotations

from afr_server import storage
from afr_server.cases import create_case, run_case_blocking
from afr_server.db import session_scope
from afr_server.transport import DirectTransport
from agent_flight_recorder import (
    EffectMode,
    EffectPolicy,
    Recorder,
    ReplayBudget,
    RunStatus,
)
from agent_flight_recorder.replay import openai_agents_adapter
from agent_flight_recorder.replay.engine import ReplayPlan, ReplaySession
from oa_sre_agent.agent import AGENT_NAME, agent_spec, run_scenario
from oa_sre_agent.scripted_model import SCRIPTED_MODEL_NAME


def record(run_id: str) -> str:
    recorder = Recorder(
        AGENT_NAME,
        transport=DirectTransport(),
        flush_interval=0.0,
        run_id=run_id,
        model=SCRIPTED_MODEL_NAME,
    )
    run_scenario(recorder)
    recorder.close()
    return run_id


def make_case(source_run_id: str, *, policy: EffectPolicy | None = None) -> str:
    with session_scope() as session:
        row = create_case(
            session,
            name=f"reason-{source_run_id}",
            source_run_id=source_run_id,
            assertions=[{"type": "no_error"}],
            from_seq=15,
            policy=policy,
        )
        return row.id


def run_and_read(case_id: str, **kwargs) -> dict:
    run_case_blocking(case_id, **kwargs)
    with session_scope() as session:
        row = storage.get_case(session, case_id)
        assert row is not None
        return {
            "status": row.last_status,
            "cause": dict(row.last_cause or {}) if row.last_cause else None,
        }


def test_blocked_side_effect_produces_its_own_code(afr_db) -> None:
    """副作用被闸门拦住：结论是 inconclusive，成因是 side_effect_blocked。"""

    parent = record("gate-parent")
    policy = EffectPolicy(
        default=EffectMode.RECORDED,
        by_kind={"model_call": EffectMode.LIVE, "tool_call": EffectMode.LIVE},
    )
    result = run_and_read(make_case(parent, policy=policy), policy=policy)

    assert result["status"] == "inconclusive"
    assert result["cause"]["code"] == "side_effect_blocked"
    assert "notify_oncall" in result["cause"]["detail"]


def test_incomplete_recording_produces_a_recording_code_not_the_gate_code(afr_db) -> None:
    """录制不完整：同样是 inconclusive，但成因码必须与副作用拦截不同。"""

    parent = record("loss-parent")
    with session_scope() as session:
        row = storage.get_run(session, parent)
        assert row is not None
        row.meta = {**(row.meta or {}), "afr_recording": {"complete": False}}
        session.add(row)

    result = run_and_read(make_case(parent))

    assert result["status"] == "inconclusive"
    assert result["cause"]["code"] == "recording_loss"
    assert result["cause"]["code"] != "side_effect_blocked"


def test_the_two_inconclusive_causes_carry_different_actions(afr_db) -> None:
    """两种成因在数据层面就是不同的：控制台据此给出不同的下一步。"""

    gated_parent = record("two-gate-parent")
    policy = EffectPolicy(by_kind={"model_call": EffectMode.LIVE, "tool_call": EffectMode.LIVE})
    gated = run_and_read(make_case(gated_parent, policy=policy), policy=policy)

    lost_parent = record("two-loss-parent")
    with session_scope() as session:
        row = storage.get_run(session, lost_parent)
        row.meta = {**(row.meta or {}), "afr_recording": {"complete": False}}
        session.add(row)
    lost = run_and_read(make_case(lost_parent))

    assert gated["status"] == lost["status"] == "inconclusive"
    assert gated["cause"]["code"] != lost["cause"]["code"]
    assert gated["cause"]["detail"] != lost["cause"]["detail"]


# ---------------------------------------------------------------- 第二个框架的守卫


def _parent_missing_one_tool_result(run_id: str):
    """录一次真实运行，然后把某一步的工具结果改成「与这次调用对不上」。"""

    record(run_id)
    with session_scope() as session:
        parent_run = storage.run_to_record(storage.get_run(session, run_id))
        events = storage.get_events(session, run_id)

    def mismatched(event):
        if event.name != "prometheus_query":
            return event
        payload = dict(event.input or {})
        args = dict(payload.get("args") or {})
        args["window"] = "31m"
        payload["args"] = args
        return event.model_copy(update={"input": payload})

    return parent_run, [mismatched(event) for event in events]


def test_missing_recorded_tool_result_becomes_a_coded_conclusion(afr_db) -> None:
    """复现时找不到那一步的录制结果：必须给出成因码，而不是一次看似成功的运行。

    这是第二个框架暴露出来的失败通道问题在端到端上的落点：Agents SDK 默认把工具异常换成
    给模型看的文本，回放会继续跑完并「成功」。

    承重点不止一处（刻意纵深：会话判定 → 适配层的返回后检查 → 边界层的成因恢复），
    因此反证要把两个恢复环节同时关掉才会红——单独关掉任一条时，另一条会接住成因。
    """

    parent_run, events = _parent_missing_one_tool_result("missing-tool-parent")
    plan = ReplayPlan.reproduce(parent_run.id, from_seq=1)
    recorder = Recorder(
        parent_run.agent_name,
        transport=DirectTransport(),
        flush_interval=0.0,
        run_id="missing-tool-replay",
    )
    result = openai_agents_adapter.run_replay(
        session=ReplaySession(plan, parent_run, events),
        recorder=recorder,
        agent_factory=agent_spec().build,
        tool_side_effects=agent_spec().tool_side_effects,
    )
    recorder.close()

    assert result.status == RunStatus.FAILED.value
    assert result.complete is False
    assert result.reason is not None
    assert result.reason.code == "missing_recorded_response"
    # 成因里带现场：是哪个工具的哪次参数没匹配上。
    assert "prometheus_query" in result.reason.detail
    # 现场信息说的是「这次要调用什么」（回放侧的真实参数），而不是录制侧旧参数：
    # 用户要能据此判断 Agent 现在的行为与录制为何不同。
    assert "30m" in result.reason.detail
    assert "本次调用参数" in result.reason.detail

    with session_scope() as session:
        row = storage.get_run(session, "missing-tool-replay")
        assert row is not None
        assert row.status == RunStatus.FAILED.value


def test_budget_stop_on_a_tool_step_keeps_its_code(afr_db) -> None:
    """预算在工具步骤上触顶：结论是 aborted + budget_exceeded，不是一次失败的运行。

    这一步的停止决定是在工具内部做出的，异常因此会经过框架的包装；
    成因必须从异常链上恢复出来，否则这里会退化成「没有成因的 failed」。
    """

    parent = record("budget-parent")
    with session_scope() as session:
        parent_run = storage.run_to_record(storage.get_run(session, parent))
        events = storage.get_events(session, parent)

    policy = EffectPolicy(
        default=EffectMode.RECORDED,
        by_kind={"model_call": EffectMode.LIVE, "tool_call": EffectMode.LIVE},
    )
    plan = ReplayPlan(
        parent_run_id=parent,
        from_seq=1,
        policy=policy,
        budget=ReplayBudget(max_model_calls=1),
    )
    recorder = Recorder(
        parent_run.agent_name,
        transport=DirectTransport(),
        flush_interval=0.0,
        run_id="budget-replay",
    )
    result = openai_agents_adapter.run_replay(
        session=ReplaySession(plan, parent_run, events),
        recorder=recorder,
        agent_factory=agent_spec().build,
        tool_side_effects=agent_spec().tool_side_effects,
    )
    recorder.close()

    assert result.status == RunStatus.ABORTED.value
    assert result.complete is False
    assert result.reason is not None
    assert result.reason.code == "budget_exceeded"
    # 记账如实：用了几次、上限几次、停在哪一维。
    assert result.budget is not None
    assert result.budget.model_calls_used == 1
    assert result.budget.max_model_calls == 1
    assert result.budget.stopped_by == "model_calls"
