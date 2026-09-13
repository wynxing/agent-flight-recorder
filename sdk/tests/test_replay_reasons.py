"""每个成因都要有**真实产生它的路径**，而不是只声明类型。

这里驱动的是真正会抛异常的代码（引擎预检、适配层的工具分支），断言的是结构化的
``code``；docs/replay-semantics.md 第 8 节的对齐清单逐条对应这些测试。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_flight_recorder.models import (
    EffectMode,
    Event,
    EventType,
    RunRecord,
    RunStatus,
    SideEffect,
    utcnow,
)
from agent_flight_recorder.replay.engine import (
    IncompleteReplay,
    ReplayDivergence,
    ReplayExhaustedError,
    ReplayPlan,
    ReplaySession,
    SideEffectBlocked,
)
from agent_flight_recorder.replay.reasons import InconclusiveCode, InconclusiveReason


def event(seq: int, event_type: EventType, **kwargs: Any) -> Event:
    return Event(id=f"e{seq}", run_id="parent", seq=seq, type=event_type, started_at=utcnow(), **kwargs)


def recording() -> list[Event]:
    return [
        event(1, EventType.RUN_STARTED, input={"task": "t"}),
        event(2, EventType.MODEL_CALL, name="m", output={"text": "hi"}),
        event(3, EventType.RUN_FINISHED, output={"result": "hi", "status": "succeeded"}),
    ]


def parent(**metadata: Any) -> RunRecord:
    return RunRecord(
        id="parent",
        agent_name="demo",
        status=RunStatus.SUCCEEDED,
        started_at=utcnow(),
        metadata=dict(metadata),
    )


def session(events: list[Event], run: RunRecord | None = None) -> ReplaySession:
    return ReplaySession(ReplayPlan.reproduce("parent", 1), run or parent(), events)


def code_of(events: list[Event], run: RunRecord | None = None) -> str:
    with pytest.raises(ReplayExhaustedError) as caught:
        session(events, run).validate_recording()
    return caught.value.cause.code


def test_engine_produces_each_declared_recording_code() -> None:
    """六类录制层面成因各自由真实输入产生，且码互不混用。"""

    assert code_of([]) == InconclusiveCode.INCOMPLETE_RECORDING.value
    assert code_of(recording()[:-1]) == InconclusiveCode.INCOMPLETE_RECORDING.value

    gapped = [item for item in recording() if item.seq != 2]
    assert code_of(gapped) == InconclusiveCode.EVENT_SEQUENCE_GAP.value

    redacted = recording()
    redacted[1].redactions = ["openai_api_key"]
    assert code_of(redacted) == InconclusiveCode.REDACTED_REPLAY_DATA.value

    assert code_of(recording(), parent(afr_recording={"complete": False})) == InconclusiveCode.RECORDING_LOSS.value

    truncated = recording()
    truncated[1].input = {"messages": [{"role": "human"}], "message_count": 30}
    assert code_of(truncated) == InconclusiveCode.TRUNCATED_CONTEXT.value

    # 缺少录制结果：引擎拒绝凭空编造一次模型响应。
    with pytest.raises(ReplayExhaustedError) as caught:
        session(recording()).recorded_model_response(999)
    assert caught.value.cause.code == InconclusiveCode.MISSING_RECORDED_RESPONSE.value


def test_missing_boundary_and_sequence_gap_are_not_one_code() -> None:
    """边界缺失与序列缺口是两种可操作方向不同的成因，不能合成同一个码。"""

    assert code_of(recording()[:-1]) != code_of([item for item in recording() if item.seq != 2])


def test_adapter_produces_missing_recorded_response_for_an_unmatched_tool() -> None:
    """适配层真的走到「父 Run 没有这次调用的录制结果」时，抛出的是结构化成因。"""

    from agent_flight_recorder.replay.langgraph_adapter import ReplayMiddleware

    class _Recorder:
        def record(self, *args: Any, **kwargs: Any) -> None:
            return None

    class _Request:
        tool_call = {"name": "read", "args": {"path": "missing.txt"}, "id": "c1"}

    middleware = ReplayMiddleware(session(recording()), _Recorder())  # type: ignore[arg-type]
    plan = middleware.session.next_step(EventType.TOOL_CALL.value, side_effect=SideEffect.READ)

    with pytest.raises(ReplayExhaustedError) as caught:
        middleware._replayed_tool_result(_Request(), plan)  # noqa: SLF001 - 驱动的正是这条分支
    assert caught.value.cause.code == InconclusiveCode.MISSING_RECORDED_RESPONSE.value
    # 码与说明分离：散文只出现在 detail，码本身是稳定的枚举取值。
    assert caught.value.cause.detail and "read" in caught.value.cause.detail


def test_cause_types_are_distinguishable_by_type_not_by_parsing_text() -> None:
    """录制不完整、复现偏离、副作用被拦是三种类型，判定不靠解析异常文本。"""

    assert issubclass(IncompleteReplay, ReplayExhaustedError)
    assert issubclass(ReplayDivergence, ReplayExhaustedError)
    assert issubclass(SideEffectBlocked, ReplayExhaustedError)

    assert IncompleteReplay("边界缺失").cause.code == InconclusiveCode.INCOMPLETE_RECORDING.value
    assert ReplayDivergence("上下文变了").cause.code == InconclusiveCode.MODEL_CONTEXT_CHANGED.value
    assert SideEffectBlocked("被拦").cause.code == InconclusiveCode.SIDE_EFFECT_BLOCKED.value


def test_exhausted_error_can_carry_an_already_structured_cause() -> None:
    """已经结构化的成因可以原样继续向上传递，不需要再拼一次文本。"""

    original = InconclusiveReason.from_code("recording_loss", "丢事件")
    assert ReplayExhaustedError(original).cause == original
