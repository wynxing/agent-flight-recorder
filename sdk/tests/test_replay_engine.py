"""回放引擎的框架无关部分：效果匹配、步骤计划与分叉判定。

这些测试完全不依赖 LangGraph，因此回放语义本身可以被独立验证。
"""

from __future__ import annotations

from agent_flight_recorder.models import (
    EffectMode,
    EffectPolicy,
    EffectSource,
    Event,
    EventType,
    RunRecord,
    RunStatus,
    SideEffect,
    utcnow,
)
from agent_flight_recorder.replay.effects import RecordedEffects, args_key
from agent_flight_recorder.replay.engine import (
    ReplayExhaustedError,
    ReplayPlan,
    ReplaySession,
    ensure_replay_context,
)
from agent_flight_recorder.replay.fork import ForkKind, behavioral_steps, classify_step, detect_fork
from agent_flight_recorder.replay.reasons import (
    CAUSE_CODES,
    InconclusiveCode,
    InconclusiveReason,
    most_significant,
)


def make_event(seq: int, event_type: EventType, **kwargs) -> Event:
    return Event(
        id=f"e{seq}",
        run_id="parent",
        seq=seq,
        type=event_type,
        started_at=utcnow(),
        **kwargs,
    )


def parent_events() -> list[Event]:
    return [
        make_event(1, EventType.RUN_STARTED, input={"task": "check latency"}),
        make_event(2, EventType.STATE_SNAPSHOT, output={"state": {"messages": []}}),
        make_event(
            3,
            EventType.MODEL_CALL,
            name="m",
            output={
                "text": "look at metrics",
                "tool_calls": [{"id": "c1", "name": "prometheus_query", "args": {"q": "latency"}}],
            },
        ),
        make_event(
            4,
            EventType.TOOL_CALL,
            name="prometheus_query",
            input={"args": {"q": "latency"}},
            output={"text": "p99 spiked at 14:02"},
            side_effect=SideEffect.READ,
        ),
        make_event(5, EventType.MODEL_CALL, name="m", output={"text": "final conclusion"}),
        make_event(
            6,
            EventType.RUN_FINISHED,
            output={"result": "final conclusion", "status": "succeeded"},
        ),
    ]


def parent_run() -> RunRecord:
    return RunRecord(id="parent", agent_name="demo", status=RunStatus.SUCCEEDED, started_at=utcnow())


def make_session(plan: ReplayPlan) -> ReplaySession:
    return ReplaySession(plan, parent_run(), parent_events())


def expect_exhausted(session: ReplaySession, fragment: str) -> None:
    try:
        session.validate_recording()
    except ReplayExhaustedError as exc:
        # 判定走结构化的码，不再靠子串匹配散文。
        assert exc.cause.code == fragment, exc.cause
    else:
        raise AssertionError(f"应当以 {fragment} 拒绝这次回放，而不是继续跑")


def test_args_key_ignores_key_order() -> None:
    assert args_key({"b": 1, "a": 2}) == args_key({"a": 2, "b": 1})


def test_mutating_step_beyond_parent_tail_uses_gate() -> None:
    policy = EffectPolicy(default=EffectMode.LIVE)
    session = ReplaySession(ReplayPlan(parent_run_id="parent", from_seq=1, policy=policy), parent_run(), [])
    step = session.next_step("tool_call", side_effect=SideEffect.WRITE)
    assert step.mode == EffectMode.DRY_RUN
    assert step.warn and step.downgraded
    policy.allow_side_effect_execution = True
    step = session.next_step("tool_call", side_effect=SideEffect.EXTERNAL)
    assert step.mode == EffectMode.LIVE
    assert step.warn


def test_recorded_tool_result_is_found_by_name_and_args() -> None:
    effects = RecordedEffects(parent_events())
    found = effects.find_tool_result("prometheus_query", {"q": "latency"})
    assert found is not None
    assert found.text == "p99 spiked at 14:02"


def test_unmatched_tool_call_returns_none_instead_of_guessing() -> None:
    effects = RecordedEffects(parent_events())
    assert effects.find_tool_result("prometheus_query", {"q": "different"}) is None
    assert effects.find_tool_result("unknown_tool", {"q": "latency"}) is None


def test_recorded_result_is_consumed_once() -> None:
    effects = RecordedEffects(parent_events())
    assert effects.find_tool_result("prometheus_query", {"q": "latency"}) is not None
    assert effects.find_tool_result("prometheus_query", {"q": "latency"}) is None


def test_model_response_lookup_by_seq() -> None:
    effects = RecordedEffects(parent_events())
    found = effects.model_response_at(3)
    assert found is not None
    assert found.text == "look at metrics"
    assert effects.model_response_at(99) is None


def test_steps_before_fork_point_are_forced_to_recorded() -> None:
    session = make_session(ReplayPlan.regress("parent", 5))

    first = session.next_step(EventType.MODEL_CALL.value)
    assert first.parent_seq == 3
    assert first.mode is EffectMode.RECORDED
    assert first.reason == "before_fork_point"

    second = session.next_step(EventType.TOOL_CALL.value)
    assert second.parent_seq == 4
    assert second.mode is EffectMode.RECORDED

    third = session.next_step(EventType.MODEL_CALL.value)
    assert third.parent_seq == 5
    assert third.mode is EffectMode.LIVE


def test_reproduce_keeps_everything_recorded() -> None:
    session = make_session(ReplayPlan.reproduce("parent", 3))
    modes = [
        session.next_step(EventType.MODEL_CALL.value).mode,
        session.next_step(EventType.TOOL_CALL.value).mode,
        session.next_step(EventType.MODEL_CALL.value).mode,
    ]
    assert modes == [EffectMode.RECORDED] * 3


def test_recorded_model_response_raises_when_missing() -> None:
    session = make_session(ReplayPlan.reproduce("parent", 1))
    try:
        session.recorded_model_response(999)
    except ReplayExhaustedError as exc:
        assert "父 Run 没有" in str(exc)
    else:
        raise AssertionError("应当拒绝在没有录制结果时编造响应")


def test_identical_steps_produce_no_fork() -> None:
    parent = make_event(3, EventType.MODEL_CALL, name="m", output={"text": "same"})
    replay = make_event(3, EventType.MODEL_CALL, name="m", output={"text": "same"})
    assert classify_step(parent, replay) is None


def test_model_text_change_is_reported() -> None:
    parent = make_event(3, EventType.MODEL_CALL, name="m", output={"text": "old"})
    replay = make_event(3, EventType.MODEL_CALL, name="m", output={"text": "new"})
    fork = classify_step(parent, replay)
    assert fork is not None
    assert fork.kind is ForkKind.MODEL_OUTPUT


def test_tool_arg_change_is_reported() -> None:
    parent = make_event(4, EventType.TOOL_CALL, name="t", input={"args": {"a": 1}})
    replay = make_event(4, EventType.TOOL_CALL, name="t", input={"args": {"a": 2}})
    fork = classify_step(parent, replay)
    assert fork is not None
    assert fork.kind is ForkKind.TOOL_ARGS


def test_different_tool_is_reported_as_sequence_change() -> None:
    parent = make_event(4, EventType.TOOL_CALL, name="a")
    replay = make_event(4, EventType.TOOL_CALL, name="b")
    fork = classify_step(parent, replay)
    assert fork is not None
    assert fork.kind is ForkKind.TOOL_SEQUENCE


def test_effect_source_difference_is_not_a_fork() -> None:
    """父 Run 是 live、回放是 recorded，这本就是回放的定义，不能算行为变化。"""

    parent = make_event(
        4,
        EventType.TOOL_CALL,
        name="t",
        input={"args": {"a": 1}},
        output={"text": "same"},
        effect_source=EffectSource.LIVE,
    )
    replay = make_event(
        4,
        EventType.TOOL_CALL,
        name="t",
        input={"args": {"a": 1}},
        output={"text": "same"},
        effect_source=EffectSource.RECORDED,
    )
    assert classify_step(parent, replay) is None


def test_detect_fork_finds_first_divergence() -> None:
    parent = parent_events()
    replay = [event.model_copy(deep=True) for event in parent]
    replay[4] = make_event(5, EventType.MODEL_CALL, name="m", output={"text": "different"})

    fork = detect_fork(parent, replay)
    assert fork is not None
    assert fork.kind is ForkKind.MODEL_OUTPUT
    assert fork.parent_seq == 5


def test_detect_fork_reports_length_difference() -> None:
    parent = parent_events()
    replay = [event.model_copy(deep=True) for event in parent[:4]]
    fork = detect_fork(parent, replay)
    assert fork is not None
    assert fork.kind is ForkKind.LENGTH


def test_state_snapshots_are_not_behavioral_steps() -> None:
    steps = behavioral_steps(parent_events())
    assert all(step.type is not EventType.STATE_SNAPSHOT for step in steps)
    assert [step.seq for step in steps] == [3, 4, 5]


def test_start_and_finish_events_are_not_behavioral_steps() -> None:
    """run_started / run_finished 是边界事件，不参与步骤对齐。"""

    kinds = {step.type for step in behavioral_steps(parent_events())}
    assert kinds == {EventType.MODEL_CALL, EventType.TOOL_CALL}


def test_error_events_do_not_shift_step_alignment() -> None:
    events = parent_events()
    events.insert(3, make_event(99, EventType.ERROR, error={"type": "X", "message": "boom"}))
    assert [step.seq for step in behavioral_steps(events)] == [3, 4, 5]


# ------------------------------------------------------------------ 显式失败
#
# 回放可信度的前提是"拿不到可信结论就显式失败"。下面五类失败各自压一条测试，
# 与 docs/replay-semantics.md 第 1 节的边界逐条对应：只要录制的证据不足以支撑
# 复现，引擎必须给出原因，而不是跑出一个看起来合理的结论。


def test_recording_without_boundaries_is_rejected() -> None:
    """父 Run 没有 run_started / run_finished 边界时不能当作可复现的录制。"""

    expect_exhausted(
        ReplaySession(ReplayPlan.reproduce("parent", 1), parent_run(), parent_events()[:-1]),
        "incomplete_recording",
    )
    expect_exhausted(
        ReplaySession(ReplayPlan.reproduce("parent", 1), parent_run(), parent_events()[1:]),
        "incomplete_recording",
    )
    expect_exhausted(
        ReplaySession(ReplayPlan.reproduce("parent", 1), parent_run(), []),
        "incomplete_recording",
    )


def test_event_sequence_gap_is_rejected() -> None:
    """seq 不连续说明事件丢过，任何一步都可能对不上，因此必须先拒绝。"""

    events = [event for event in parent_events() if event.seq != 4]
    expect_exhausted(
        ReplaySession(ReplayPlan.reproduce("parent", 1), parent_run(), events),
        "event_sequence_gap",
    )


def test_redacted_recording_is_rejected() -> None:
    """发生过脱敏就意味着证据不再逐字可比，复现结论会失真。"""

    events = parent_events()
    events[2].redactions = ["openai_api_key"]
    expect_exhausted(
        ReplaySession(ReplayPlan.reproduce("parent", 1), parent_run(), events),
        "redacted_replay_data",
    )

    redacted_run = parent_run()
    redacted_run.redactions = ["authorization"]
    expect_exhausted(
        ReplaySession(ReplayPlan.reproduce("parent", 1), redacted_run, parent_events()),
        "redacted_replay_data",
    )


def test_recording_loss_marker_is_rejected() -> None:
    """SDK 自己声明录制丢过事件时，父 Run 的证据就是不完整的。"""

    run = parent_run()
    run.metadata["afr_recording"] = {"complete": False, "events_dropped": 2}
    expect_exhausted(
        ReplaySession(ReplayPlan.reproduce("parent", 1), run, parent_events()),
        "recording_loss",
    )


def test_truncated_model_input_is_rejected() -> None:
    """message_count 大于实际保存的 messages，说明喂给模型的上下文被截断了。"""

    events = parent_events()
    events[2].input = {"messages": [{"role": "human", "content": "only one"}], "message_count": 30}
    expect_exhausted(
        ReplaySession(ReplayPlan.reproduce("parent", 1), parent_run(), events),
        "truncated_context",
    )


def test_exhausted_recording_stops_later_steps() -> None:
    """验证过的失败必须真的拦住后续步骤，而不是只在开头记一笔。"""

    session = ReplaySession(ReplayPlan.reproduce("parent", 1), parent_run(), parent_events())
    session.incomplete_reason = InconclusiveReason.from_code("recording_loss")
    try:
        session.next_step(EventType.MODEL_CALL.value)
    except ReplayExhaustedError as exc:
        assert exc.cause.code == "recording_loss"
    else:
        raise AssertionError("盘面已经判为不完整，就不该继续解析步骤")


def test_initial_state_is_required_when_the_parent_recorded_one() -> None:
    """父 Run 记了初始 input，引擎不会假装状态已经恢复。"""

    try:
        ensure_replay_context(
            initial_state=None,
            parent_metadata={},
            initial_input={"alert": "checkout-api p99"},
        )
    except ReplayExhaustedError as exc:
        assert exc.cause.code == "missing_initial_state"
    else:
        raise AssertionError("缺少 initial_state 时不该继续复现")


def test_initial_state_guard_accepts_the_two_declared_paths() -> None:
    """要么真的给了状态，要么录制方显式声明只跑 task 消息。"""

    # 显式给了 initial_state：引擎恢复的是真实状态。
    ensure_replay_context(
        initial_state={"messages": []},
        parent_metadata={},
        initial_input={"alert": "checkout-api p99"},
    )
    # 录制方显式打了 task_only 标记：这是录制契约的一部分，不是引擎的猜测。
    ensure_replay_context(
        initial_state=None,
        parent_metadata={"afr_replay_context": "task_only"},
        initial_input={"alert": "checkout-api p99"},
    )
    # 父 Run 本来就没记 input：没有需要恢复的状态。
    ensure_replay_context(initial_state=None, parent_metadata={}, initial_input=None)
