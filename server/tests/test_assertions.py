"""确定性断言。Eval 的可信度全部建立在这里。"""

from __future__ import annotations

from agent_flight_recorder.models import ErrorInfo, Event, EventType, utcnow
from afr_server.assertions import AssertionSpec, evaluate, evaluate_assertions


def event(seq: int, event_type: EventType, **kwargs) -> Event:
    return Event(
        id=f"e{seq}",
        run_id="r",
        seq=seq,
        type=event_type,
        started_at=utcnow(),
        **kwargs,
    )


def sample_events() -> list[Event]:
    return [
        event(1, EventType.TOOL_CALL, name="prometheus_query", input={"args": {"q": "latency"}}),
        event(2, EventType.TOOL_CALL, name="notify_oncall", input={"args": {"channel": "#sre"}}),
    ]


def test_no_error_passes_and_fails_correctly() -> None:
    assert evaluate(AssertionSpec(type="no_error"), sample_events(), "ok").passed is True

    with_error = sample_events() + [event(3, EventType.ERROR, error=ErrorInfo(type="X", message="boom"))]
    result = evaluate(AssertionSpec(type="no_error"), with_error, "ok")
    assert result.passed is False
    assert "boom" in result.detail


def test_tool_called_matches_name_and_args() -> None:
    spec = AssertionSpec(type="tool_called", tool="prometheus_query")
    assert evaluate(spec, sample_events(), "").passed is True

    with_args = AssertionSpec(
        type="tool_called", tool="prometheus_query", args_contains={"q": "late"}
    )
    assert evaluate(with_args, sample_events(), "").passed is True

    wrong_args = AssertionSpec(
        type="tool_called", tool="prometheus_query", args_contains={"q": "cpu"}
    )
    assert evaluate(wrong_args, sample_events(), "").passed is False


def test_tool_not_called() -> None:
    spec = AssertionSpec(type="tool_not_called", tool="kubectl_delete")
    assert evaluate(spec, sample_events(), "").passed is True
    assert evaluate(AssertionSpec(type="tool_not_called", tool="notify_oncall"), sample_events(), "").passed is False


def test_final_output_assertions() -> None:
    assert evaluate(AssertionSpec(type="final_output_contains", value="REDIS"), sample_events(), "fix REDIS pool").passed
    assert not evaluate(AssertionSpec(type="final_output_contains", value="redis"), sample_events(), "nothing").passed
    assert evaluate(AssertionSpec(type="final_output_not_contains", value="redis"), sample_events(), "nothing").passed
    assert evaluate(AssertionSpec(type="final_output_matches", value=r"根因.*回归"), sample_events(), "根因：配置回归").passed


def test_final_output_contains_is_case_insensitive() -> None:
    spec = AssertionSpec(type="final_output_contains", value="redis_pool_size")
    assert evaluate(spec, sample_events(), "回滚 REDIS_POOL_SIZE 到 64").passed is True


def test_tool_sequence_equals() -> None:
    spec = AssertionSpec(type="tool_sequence_equals", value=["prometheus_query", "notify_oncall"])
    assert evaluate(spec, sample_events(), "").passed is True
    assert evaluate(AssertionSpec(type="tool_sequence_equals", value=["notify_oncall"]), sample_events(), "").passed is False


def test_max_tool_calls() -> None:
    assert evaluate(AssertionSpec(type="max_tool_calls", value=2), sample_events(), "").passed is True
    assert evaluate(AssertionSpec(type="max_tool_calls", value=1), sample_events(), "").passed is False


def test_invalid_regex_is_reported_not_crashed() -> None:
    result = evaluate(AssertionSpec(type="final_output_matches", value="[unclosed"), sample_events(), "text")
    assert result.passed is False
    assert "正则无效" in result.detail


def test_unknown_assertion_type_fails_loudly() -> None:
    result = evaluate(AssertionSpec(type="does_not_exist"), sample_events(), "")
    assert result.passed is False
    assert "未知断言类型" in result.detail


def test_evaluate_accepts_raw_dicts() -> None:
    results = evaluate_assertions([{"type": "no_error"}], sample_events(), "ok")
    assert len(results) == 1
    assert results[0].passed is True


def test_every_assertion_type_is_describable() -> None:
    from afr_server.assertions import SUPPORTED_TYPES

    for assertion_type in SUPPORTED_TYPES:
        text = AssertionSpec(type=assertion_type, value=1, tool="t").describe()
        assert text

