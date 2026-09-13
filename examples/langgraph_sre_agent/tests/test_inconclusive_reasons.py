"""端到端：真实 Agent 上，「拿不到结论」与「副作用被拦截」是两种不同的判定。

这条链路走的是真实的录制 → 回放 → 用例判定 → 落库，不是只测类型：

* 录制丢失 -> 用例 inconclusive，成因是录制层面的码；
* 副作用被闸门拦截 -> 用例 inconclusive，成因是 `side_effect_blocked`。

两者结论同为「无法判断」，但成因码必须不同——否则控制台只能给用户看四个字，
而不知道让他去看录制质量还是去看副作用策略。
"""

from __future__ import annotations

from agent_flight_recorder import EffectMode, EffectPolicy, Recorder
from afr_server.cases import create_case, run_case_blocking
from afr_server.db import session_scope
from afr_server.storage import get_case, get_run
from afr_server.transport import DirectTransport
from sre_agent.agent import AGENT_NAME, run_scenario


def record(run_id: str) -> str:
    recorder = Recorder(AGENT_NAME, transport=DirectTransport(), flush_interval=0.0, run_id=run_id)
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
        row = get_case(session, case_id)
        assert row is not None
        return {"status": row.last_status, "cause": dict(row.last_cause or {}) if row.last_cause else None}


def test_blocked_side_effect_produces_its_own_code(afr_db) -> None:
    """副作用被闸门拦住：结论是 inconclusive，成因是 side_effect_blocked。"""

    parent = record("gate-parent")
    # 工具策略要求真实执行：写操作与对外动作会被闸门降级为拦截。
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
        row = get_run(session, parent)
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
        row = get_run(session, lost_parent)
        row.meta = {**(row.meta or {}), "afr_recording": {"complete": False}}
        session.add(row)
    lost = run_and_read(make_case(lost_parent))

    assert gated["status"] == lost["status"] == "inconclusive"
    assert gated["cause"]["code"] != lost["cause"]["code"]
    # 成因里带的是现场信息（哪个工具），不是一句无法行动的「无法判断」。
    assert gated["cause"]["detail"] != lost["cause"]["detail"]
