"""协议模型与 effect policy 的解析规则。"""

from agent_flight_recorder.models import (
    EffectMode,
    EffectPolicy,
    Event,
    EventType,
    SideEffect,
    summarize_events,
    utcnow,
)


def test_default_policy_is_recorded() -> None:
    policy = EffectPolicy()
    assert policy.resolve_mode(seq=1, kind=EventType.MODEL_CALL.value) is EffectMode.RECORDED


def test_priority_is_seq_then_kind_then_default() -> None:
    policy = EffectPolicy(
        default=EffectMode.DRY_RUN,
        by_kind={EventType.TOOL_CALL.value: EffectMode.LIVE},
        by_seq={7: EffectMode.RECORDED},
    )
    assert policy.resolve_mode(seq=7, kind=EventType.TOOL_CALL.value) is EffectMode.RECORDED
    assert policy.resolve_mode(seq=8, kind=EventType.TOOL_CALL.value) is EffectMode.LIVE
    assert policy.resolve_mode(seq=8, kind=EventType.MODEL_CALL.value) is EffectMode.DRY_RUN


def test_reproduce_uses_only_recordings() -> None:
    policy = EffectPolicy.reproduce()
    for kind in (EventType.MODEL_CALL.value, EventType.TOOL_CALL.value):
        assert policy.resolve_mode(seq=11, kind=kind) is EffectMode.RECORDED


def test_regress_runs_model_but_replays_tools() -> None:
    policy = EffectPolicy.regress()
    assert policy.resolve_mode(seq=3, kind=EventType.MODEL_CALL.value) is EffectMode.LIVE
    assert policy.resolve_mode(seq=3, kind=EventType.TOOL_CALL.value) is EffectMode.RECORDED


def test_side_effect_gate_downgrades_live_writes() -> None:
    policy = EffectPolicy(by_kind={EventType.TOOL_CALL.value: EffectMode.LIVE})

    blocked = policy.resolve(seq=5, kind=EventType.TOOL_CALL.value, side_effect=SideEffect.EXTERNAL)
    assert blocked.mode is EffectMode.DRY_RUN
    assert blocked.downgraded is True
    assert blocked.reason == "side_effect_gate"

    allowed_read = policy.resolve(seq=5, kind=EventType.TOOL_CALL.value, side_effect=SideEffect.READ)
    assert allowed_read.mode is EffectMode.LIVE
    assert allowed_read.downgraded is False


def test_explicit_permission_allows_but_still_warns() -> None:
    policy = EffectPolicy(
        by_kind={EventType.TOOL_CALL.value: EffectMode.LIVE},
        allow_side_effect_execution=True,
    )
    decision = policy.resolve(seq=5, kind=EventType.TOOL_CALL.value, side_effect=SideEffect.WRITE)
    assert decision.mode is EffectMode.LIVE
    assert decision.warn is True
    assert decision.reason == "side_effect_executed"


def test_gate_does_not_touch_model_calls() -> None:
    policy = EffectPolicy(by_kind={EventType.MODEL_CALL.value: EffectMode.LIVE})
    decision = policy.resolve(seq=1, kind=EventType.MODEL_CALL.value, side_effect=None)
    assert decision.mode is EffectMode.LIVE
    assert decision.downgraded is False


def _event(seq: int, event_type: EventType, **kwargs) -> Event:
    return Event(
        id=f"e{seq}",
        run_id="r1",
        seq=seq,
        type=event_type,
        started_at=utcnow(),
        **kwargs,
    )


def test_summary_counts_effect_sources() -> None:
    from agent_flight_recorder.models import EffectSource

    events = [
        _event(1, EventType.TOOL_CALL, effect_source=EffectSource.RECORDED),
        _event(2, EventType.TOOL_CALL, effect_source=EffectSource.RECORDED),
        _event(3, EventType.MODEL_CALL, effect_source=EffectSource.LIVE),
    ]
    summary = summarize_events(events)
    assert summary.tool_calls == 2
    assert summary.model_calls == 1
    assert summary.effect_counts == {"recorded": 2, "live": 1}


def test_side_effect_mutating_flag() -> None:
    assert SideEffect.READ.is_mutating is False
    assert SideEffect.WRITE.is_mutating is True
    assert SideEffect.EXTERNAL.is_mutating is True

