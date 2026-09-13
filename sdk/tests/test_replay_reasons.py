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
    ReplayExhaustedError,
    ReplayPlan,
    ReplaySession,
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


def _failure(events: list[Event], run: RunRecord | None = None) -> ReplayExhaustedError:
    """驱动引擎预检并原样返回异常：类型与成因都要能被断言，而不是只看码。"""

    with pytest.raises(ReplayExhaustedError) as caught:
        session(events, run).validate_recording()
    return caught.value


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


def test_side_effect_executed_is_never_relabelled_as_blocked() -> None:
    """``side_effect_executed`` 说反话的代价由调用方承担，因此不能落成 blocked。

    它以前是别名表里指向 ``side_effect_blocked`` 的一条——语义恰好相反。当前没有
    产生路径会把它喂进成因解析，但这是公开函数：一旦第三方适配器把步骤级 reason
    传进来，就会得到与事实相反的成因。现在这条别名被删掉，取值诚实地落到
    ``unknown`` 并把原文留在 ``detail``。
    """

    parsed = InconclusiveReason.legacy("side_effect_executed")
    # 先钉住「不是 blocked」：删别名时最容易犯的错是换成另一个听起来接近的码。
    assert parsed.code != InconclusiveCode.SIDE_EFFECT_BLOCKED.value
    assert parsed.code == InconclusiveCode.UNKNOWN.value
    # 认不出来也不能把原文弄丢。
    assert parsed.detail == "side_effect_executed"
    # 出现在整句里同样不猜。
    assert InconclusiveReason.legacy("side_effect_executed: step 4").code == "unknown"

    # 真正的拦截信号不受影响：「被拦截」与「执行了副作用」本来就是两件事。
    assert InconclusiveReason.legacy("side_effect_blocked").code == "side_effect_blocked"
    assert InconclusiveReason.legacy("side_effect_gate").code == "side_effect_blocked"


def test_replay_failures_are_classified_by_code_not_by_exception_type() -> None:
    """分类只有一个出口：成因走 ``cause.code``，不靠异常类型区分。

    引擎预检每次抛的都是同一个异常类型，成因的区分完全由结构化的 ``code`` 承担。
    以前旁边有三个从不被抛出的子类，文档说它们用来区分成因，实际生产代码一处都没抛。
    """

    failures = [
        (_failure([]), InconclusiveCode.INCOMPLETE_RECORDING.value),
        (
            _failure([item for item in recording() if item.seq != 2]),
            InconclusiveCode.EVENT_SEQUENCE_GAP.value,
        ),
        (
            _failure(recording(), parent(afr_recording={"complete": False})),
            InconclusiveCode.RECORDING_LOSS.value,
        ),
    ]

    for exc, expected in failures:
        # 类型不参与区分：三次都是同一个异常类型……
        assert type(exc) is ReplayExhaustedError
        # ……成因由 code 决定。
        assert exc.cause.code == expected
    # 而且这些成因确实互不相同，不是同一个码换了名字。
    assert len({code for _, code in failures}) == len(failures)
    # 「按异常类型区分成因」这个出口没有被悄悄加回来：它只有这一个类型。
    assert ReplayExhaustedError.__subclasses__() == []


def test_exhausted_error_can_carry_an_already_structured_cause() -> None:
    """已经结构化的成因可以原样继续向上传递，不需要再拼一次文本。"""

    original = InconclusiveReason.from_code("recording_loss", "丢事件")
    assert ReplayExhaustedError(original).cause == original
