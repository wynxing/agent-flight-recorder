"""框架适配层的边界：runtime 解析、成因恢复、失败收尾。

这三点都是第二个框架（OpenAI Agents SDK）接入时才成立的契约，因此单独一组：

* 平台按 Agent 声明的 runtime 选适配层，未知取值要说清楚，而不是悄悄回落到 LangGraph；
* 「成因」不依赖「框架原样抛出异常」——Agents SDK 会把它包一层；判定只能看
  异常链与会话状态，两者都与框架无关；
* 失败收尾只有一条路径，两个适配层共用它，形状与接入第二个框架之前逐字一致。

本文件刻意不 import 任何框架：它验证的正是「核心不需要框架也能判定」。
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from agent_flight_recorder import (
    EffectMode,
    EffectPolicy,
    Event,
    EventType,
    Recorder,
    ReplayBudget,
    RunRecord,
    RunStatus,
)
from agent_flight_recorder.models import new_id, utcnow
from agent_flight_recorder.replay import adapters as adapter_registry
from agent_flight_recorder.replay import langgraph_adapter, openai_agents_adapter
from agent_flight_recorder.replay.boundary import cause_of, finish_failed_replay
from agent_flight_recorder.replay.engine import (
    ReplayExhaustedError,
    ReplayPlan,
    ReplaySession,
)
from agent_flight_recorder.replay.reasons import InconclusiveCode, InconclusiveReason


def _parent_recording() -> tuple[RunRecord, list[Event]]:
    """一份最小的父 Run：一次模型调用、一次工具调用、一次收尾。"""

    run_id = "parent-run"
    parent = RunRecord(id=run_id, agent_name="probe", status=RunStatus.SUCCEEDED, started_at=utcnow())
    return parent, [
        Event(id=new_id(), run_id=run_id, seq=1, type=EventType.RUN_STARTED, started_at=utcnow()),
        Event(
            id=new_id(),
            run_id=run_id,
            seq=2,
            type=EventType.MODEL_CALL,
            name="probe-model",
            output={"text": "", "tool_calls": [{"id": "call_1", "name": "probe_tool", "args": {}}]},
            started_at=utcnow(),
        ),
        Event(
            id=new_id(),
            run_id=run_id,
            seq=3,
            type=EventType.TOOL_CALL,
            name="probe_tool",
            input={"args": {}},
            output={"text": "ok", "content": "ok"},
            started_at=utcnow(),
        ),
        Event(
            id=new_id(),
            run_id=run_id,
            seq=4,
            type=EventType.MODEL_CALL,
            name="probe-model",
            output={"text": "done", "tool_calls": []},
            started_at=utcnow(),
        ),
        Event(id=new_id(), run_id=run_id, seq=5, type=EventType.RUN_FINISHED, started_at=utcnow()),
    ]


def _session(*, budget: ReplayBudget | None = None) -> ReplaySession:
    parent, events = _parent_recording()
    plan = ReplayPlan(
        parent_run_id=parent.id,
        from_seq=1,
        policy=EffectPolicy(default=EffectMode.LIVE),
        budget=budget or ReplayBudget(),
    )
    return ReplaySession(plan, parent, events)


# ------------------------------------------------------------------ runtime 解析


def test_default_adapter_is_the_one_that_existed_before_the_field() -> None:
    """没声明 runtime 的 Agent 必须走原来的适配层，这是默认值的全部意义。"""

    assert adapter_registry.DEFAULT_ADAPTER == "langgraph"
    assert adapter_registry.load_adapter(None) is langgraph_adapter
    assert adapter_registry.load_adapter("") is langgraph_adapter
    assert set(adapter_registry.adapter_names()) == {"langgraph", "openai-agents"}
    assert adapter_registry.load_adapter("openai-agents") is openai_agents_adapter


def test_unknown_runtime_is_a_configuration_error() -> None:
    """未知 runtime 不能被当成 LangGraph 悄悄跑掉：那会让平台说假话。"""

    with pytest.raises(adapter_registry.UnknownAdapterError) as excinfo:
        adapter_registry.load_adapter("some-other-framework")

    message = str(excinfo.value)
    assert "some-other-framework" in message
    assert "langgraph" in message and "openai-agents" in message


def test_missing_framework_dependency_is_reported_not_swallowed(monkeypatch) -> None:
    """适配器模块装不上时要说清缺哪个包，而不是在 import 期把整个 SDK 崩掉。"""

    monkeypatch.setitem(
        adapter_registry.ADAPTER_MODULES, "ghost", "agent_flight_recorder.not_a_module"
    )

    with pytest.raises(adapter_registry.AdapterUnavailableError) as excinfo:
        adapter_registry.load_adapter("ghost")

    assert "ghost" in str(excinfo.value)


def test_sdk_core_stays_framework_free() -> None:
    """SDK 核心只依赖 pydantic：注册了第二个框架也不能把框架拖进核心包。"""

    code = (
        "import sys, agent_flight_recorder, agent_flight_recorder.replay as replay;"
        "loaded = sorted(name for name in ('langchain', 'langgraph', 'agents') "
        "if name in sys.modules);"
        "print(loaded)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == "[]"


# ------------------------------------------------------------------ 成因恢复


def test_cause_is_recovered_from_a_wrapped_engine_error() -> None:
    """框架把引擎异常包一层之后，成因仍然必须能被恢复出来。

    这正是第二个框架暴露出来的缺口：LangGraph 原样抛出，Agents SDK 会包成 UserError
    （原始异常只剩在 __cause__ 上）。没有这条恢复路径，同一份录制在两个框架上会得到
    两个不同的结论。
    """

    engine_error = ReplayExhaustedError(
        "父 Run 没有可用于复现的模型输出",
        code=InconclusiveCode.MISSING_RECORDED_RESPONSE.value,
    )
    wrapped = RuntimeError("Error running tool")
    wrapped.__cause__ = engine_error

    cause = cause_of(wrapped)

    assert cause is not None
    assert cause.code == "missing_recorded_response"
    assert "没有可用于复现" in cause.detail


def test_cause_is_recovered_through_the_implicit_context_chain() -> None:
    """框架用隐式上下文（没有 raise ... from）时同样要认出来。"""

    wrapper = RuntimeError("boom")
    wrapper.__context__ = ReplayExhaustedError(code=InconclusiveCode.EVENT_SEQUENCE_GAP.value)

    cause = cause_of(wrapper)

    assert cause is not None
    assert cause.code == "event_sequence_gap"


def test_cause_falls_back_to_the_session_state() -> None:
    """异常对象整个被吃掉时，退回到引擎自己记下的判定。"""

    session = _session()
    session.incomplete_reason = InconclusiveReason.from_code(
        InconclusiveCode.BUDGET_EXCEEDED.value, "已达到模型调用上限"
    )

    cause = cause_of(RuntimeError("框架把异常弄丢了"), session=session)

    assert cause is not None
    assert cause.code == "budget_exceeded"


def test_no_cause_is_never_invented() -> None:
    """恢复不出来就说没有，绝不猜一个已知码。"""

    assert cause_of(RuntimeError("普通异常")) is None
    assert cause_of(RuntimeError("普通异常"), session=_session()) is None


# ------------------------------------------------------------------ 失败收尾


def test_failed_replay_without_a_cause_keeps_the_frozen_shape() -> None:
    """没有成因的失败：failed + error 文本，且 complete 维持既有取值。"""

    recorder = Recorder("probe", run_id="replay-run", enabled=False, flush_interval=0.0)

    result = finish_failed_replay(session=_session(), recorder=recorder, exc=ValueError("坏了"))

    assert result.status == RunStatus.FAILED.value
    assert result.complete is True
    assert result.reason is None
    assert result.error == "ValueError: 坏了"
    recorder.close()


def test_failed_replay_with_a_cause_is_incomplete_and_keeps_the_code() -> None:
    session = _session(budget=ReplayBudget(max_model_calls=1))
    session.incomplete_reason = InconclusiveReason.from_code(
        InconclusiveCode.BUDGET_EXCEEDED.value, "已达到模型调用上限"
    )
    recorder = Recorder("probe", run_id="replay-run", enabled=False, flush_interval=0.0)

    result = finish_failed_replay(
        session=session,
        recorder=recorder,
        exc=ReplayExhaustedError(session.incomplete_reason),
    )

    assert result.complete is False
    assert result.reason is not None and result.reason.code == "budget_exceeded"
    # 预算触顶 = 没跑完，不是失败：状态是 aborted，与 pi 侧一致。
    assert result.status == RunStatus.ABORTED.value
    assert result.budget is not None and result.budget.max_model_calls == 1
    recorder.close()
