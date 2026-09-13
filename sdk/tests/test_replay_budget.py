"""回放预算：事先可预估、执行中硬停、事后如实记账。

这里驱动的是引擎真的会抛异常的那条路径（步边界上的预算判定），而不是只看类型声明；
两条最容易做歪的地方各有一组测试钉住：**超限不是失败**，**成本未知不是 0**。
"""

from __future__ import annotations

import pytest

from agent_flight_recorder.models import (
    EffectMode,
    Event,
    EventType,
    ReplayBudget,
    RunRecord,
    RunStatus,
    TokenUsage,
    utcnow,
)
from agent_flight_recorder.replay.budget import (
    STOPPED_BY_COST,
    STOPPED_BY_MODEL_CALLS,
    estimate_replay_budget,
)
from agent_flight_recorder.replay.engine import (
    ReplayExhaustedError,
    ReplayPlan,
    ReplaySession,
)
from agent_flight_recorder.replay.reasons import InconclusiveCode


def event(seq: int, event_type: EventType, **kwargs) -> Event:
    return Event(id=f"e{seq}", run_id="parent", seq=seq, type=event_type, started_at=utcnow(), **kwargs)


def recording() -> list[Event]:
    """两次模型调用夹着一次工具调用，token 记录齐全。"""

    return [
        event(1, EventType.RUN_STARTED, input={"task": "t"}),
        event(
            2,
            EventType.MODEL_CALL,
            name="gpt-4o",
            output={"text": "a"},
            tokens=TokenUsage(input=1000, output=500),
        ),
        event(3, EventType.TOOL_CALL, name="read", input={"args": {"path": "x"}}, output={"text": "y"}),
        event(
            4,
            EventType.MODEL_CALL,
            name="gpt-4o",
            output={"text": "b"},
            tokens=TokenUsage(input=2000, output=1000),
        ),
        event(5, EventType.RUN_FINISHED, output={"result": "b", "status": "succeeded"}),
    ]


def parent(**kwargs) -> RunRecord:
    return RunRecord(id="parent", agent_name="demo", status=RunStatus.SUCCEEDED, started_at=utcnow(), **kwargs)


def session(plan: ReplayPlan, run: RunRecord | None = None) -> ReplaySession:
    return ReplaySession(plan, run or parent(model="gpt-4o"), recording())


# ------------------------------------------------------------------ 事先可预估


def test_estimate_counts_the_live_model_calls_and_prices_them() -> None:
    """回归模式下分叉点之后的模型调用会真跑，预估把它们与成本一起算出来。"""

    estimate = estimate_replay_budget(ReplayPlan.regress("parent", 1), parent(model="gpt-4o"), recording())

    assert estimate.model_calls == 2
    assert estimate.cost_is_estimate is True
    # gpt-4o：输入 $2.50 / 1M，输出 $10.00 / 1M。
    expected = round((1000 / 1e6) * 2.5 + (500 / 1e6) * 10.0 + (2000 / 1e6) * 2.5 + (1000 / 1e6) * 10.0, 6)
    assert estimate.cost_usd == expected


def test_estimate_is_zero_when_nothing_runs_live() -> None:
    """复现模式本来就不花模型钱：0 次、$0 是事实，不是猜测。"""

    estimate = estimate_replay_budget(ReplayPlan.reproduce("parent", 1), parent(model="gpt-4o"), recording())

    assert estimate.model_calls == 0
    assert estimate.cost_usd == 0.0


def test_estimate_says_unknown_instead_of_inventing_a_number() -> None:
    """模型不在价格表内、或缺 token 记录时，成本是「无法预估」，不是 0。"""

    unpriced = estimate_replay_budget(
        ReplayPlan.regress("parent", 1), parent(model="afr-scripted-sre-v1"), recording()
    )
    assert unpriced.model_calls == 2
    assert unpriced.cost_usd is None
    assert "无法预估" in unpriced.detail

    no_tokens = recording()
    no_tokens[1].tokens = None
    missing = estimate_replay_budget(ReplayPlan.regress("parent", 1), parent(model="gpt-4o"), no_tokens)
    assert missing.model_calls == 2
    assert missing.cost_usd is None
    assert "token" in missing.detail


# ------------------------------------------------------------------ 执行中硬停


def test_budget_stops_at_the_step_boundary_through_a_real_production_path() -> None:
    """达到上限时引擎真的在步边界停下，并给出 ``budget_exceeded``。"""

    live = session(ReplayPlan.regress("parent", 1, budget=ReplayBudget(max_model_calls=1)))
    assert live.next_step(EventType.MODEL_CALL.value).mode is EffectMode.LIVE
    # 已经发出的那次调用允许完成并如实记账，不做预测性中断。
    live.record_live_model_call(0.01)

    with pytest.raises(ReplayExhaustedError) as caught:
        live.next_step(EventType.MODEL_CALL.value)

    assert caught.value.cause.code == InconclusiveCode.BUDGET_EXCEEDED.value
    # 码不是被写死进来的：它由引擎在真实的步边界上产生。
    assert caught.value.cause.code != InconclusiveCode.UNKNOWN.value
    # detail 带上实际用量，让人知道停在哪。
    assert "已用 1 次" in caught.value.cause.detail
    assert "上限 1 次" in caught.value.cause.detail
    # 触顶的那一维是稳定的机器取值，控制台与元数据都认它。
    assert live.budget_usage().stopped_by == STOPPED_BY_MODEL_CALLS
    # 被拦下的那一步没有进入已执行步骤，也没有多发出一次调用。
    assert [step.parent_seq for step in live.steps] == [2]
    assert live.ledger.model_calls_used == 1

    # 盘面已经判为触顶：后续步骤不会再被解析成可执行的真实调用。
    with pytest.raises(ReplayExhaustedError) as again:
        live.next_step(EventType.TOOL_CALL.value)
    assert again.value.cause.code == InconclusiveCode.BUDGET_EXCEEDED.value


def test_cost_limit_stops_a_run_that_already_spent_more_than_the_cap() -> None:
    """成本上限按「已经花掉的」判定，代价已经发生时如实入账。"""

    live = session(ReplayPlan.regress("parent", 1, budget=ReplayBudget(max_cost_usd=0.01)))
    assert live.next_step(EventType.MODEL_CALL.value).mode is EffectMode.LIVE
    live.record_live_model_call(0.02)

    with pytest.raises(ReplayExhaustedError) as caught:
        live.next_step(EventType.MODEL_CALL.value)
    assert caught.value.cause.code == InconclusiveCode.BUDGET_EXCEEDED.value

    usage = live.budget_usage()
    assert usage.stopped_by == STOPPED_BY_COST
    assert usage.exceeded is True
    assert usage.cost_used_usd == 0.02
    assert usage.max_cost_usd == 0.01
    assert usage.model_calls_used == 1


def test_reproduce_mode_is_untouched_even_with_the_strictest_budget() -> None:
    """复现模式全程读录制结果：上限声明得再小也不会被误判成触顶。"""

    replay = session(
        ReplayPlan.reproduce("parent", 1, budget=ReplayBudget(max_model_calls=0, max_cost_usd=0))
    )
    modes = [
        replay.next_step(EventType.MODEL_CALL.value).mode,
        replay.next_step(EventType.TOOL_CALL.value).mode,
        replay.next_step(EventType.MODEL_CALL.value).mode,
    ]
    assert modes == [EffectMode.RECORDED] * 3

    usage = replay.budget_usage()
    assert usage.model_calls_used == 0
    assert usage.cost_used_usd == 0.0
    assert usage.exceeded is False
    assert usage.stopped_by is None


# ------------------------------------------------------------------ 事后如实记账


def test_unknown_cost_is_never_booked_as_zero() -> None:
    """成本未知时是「未知」：既不能说成没花钱，也不能因算不出来就假装触顶。"""

    live = session(
        ReplayPlan.regress("parent", 1, budget=ReplayBudget(max_cost_usd=0.01)),
        parent(model="afr-scripted-sre-v1"),
    )
    live.next_step(EventType.MODEL_CALL.value)
    live.record_live_model_call(None)  # 模型不在本地价格表内：成本未知

    # 成本上限这一维无法判定，因此不会因为「算不出来」就停下。
    assert live.next_step(EventType.MODEL_CALL.value).mode is EffectMode.LIVE

    usage = live.budget_usage()
    assert usage.cost_used_usd is None
    assert usage.cost_unknown is True
    assert usage.exceeded is False
    assert "未知" in usage.detail


def test_declared_budget_is_reported_even_when_the_replay_finishes() -> None:
    """没被停止也要给出对照：已用 / 上限都在，触顶是 False。"""

    live = session(ReplayPlan.regress("parent", 1, budget=ReplayBudget(max_model_calls=5)))
    live.next_step(EventType.MODEL_CALL.value)
    live.record_live_model_call(0.02)

    result = live.to_result(status=RunStatus.SUCCEEDED.value)
    assert result.budget is not None
    assert result.budget.exceeded is False
    assert result.budget.stopped_by is None
    assert result.budget.model_calls_used == 1
    assert result.budget.max_model_calls == 5
    assert result.budget.cost_used_usd == 0.02


def test_a_replay_without_a_declared_budget_keeps_no_bookkeeping() -> None:
    """没声明上限就不该凭空多出一个「上限：无」的账目：行为与以前逐字一致。"""

    live = session(ReplayPlan.regress("parent", 1))
    live.next_step(EventType.MODEL_CALL.value)
    live.record_live_model_call(0.02)

    assert live.plan.budget.is_set is False
    assert live.to_result(status=RunStatus.SUCCEEDED.value).budget is None


def test_budget_defaults_are_both_unset() -> None:
    """「没声明」不等于「上限为 0」：两个维度默认都是不参与判定。"""

    budget = ReplayBudget()
    assert budget.max_cost_usd is None
    assert budget.max_model_calls is None
    assert budget.is_set is False
    assert ReplayPlan.reproduce("parent", 1).budget.is_set is False
    assert ReplayBudget(max_model_calls=0).is_set is True

