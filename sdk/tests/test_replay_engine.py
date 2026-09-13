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
from agent_flight_recorder.replay.engine import ReplayExhaustedError, ReplayPlan, ReplaySession
from agent_flight_recorder.replay.fork import ForkKind, behavioral_steps, classify_step, detect_fork


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


def test_args_key_ignores_key_order() -> None:
    assert args_key({"b": 1, "a": 2}) == args_key({"a": 2, "b": 1})


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
