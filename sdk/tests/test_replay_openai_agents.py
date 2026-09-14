"""Agents SDK 专有的两处失败语义：工具失败不能被静默吃掉。

对照表（见 docs/architecture.md 第 8 节「框架适配边界」）：

    LangGraph          工具异常冒到中间件 -> 可以如实记下、可以如实失败
    Agents SDK(默认)   工具异常被换成一句给模型看的文本 -> 一切照常往下跑

因此录制侧要识别「这一次返回其实是框架把异常换成的文本」，回放的真实执行侧要把它
当成失败。两条都配了会真变红的断言：把 openai_agents.sdk_converted_a_failure 的
判定去掉，或把回放适配层的判定去掉，这里就会红。
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_flight_recorder import (
    EffectMode,
    EffectPolicy,
    Event,
    EventType,
    IngestRequest,
    IngestResponse,
    Recorder,
    RunRecord,
    RunStatus,
    SideEffect,
)
from agent_flight_recorder.models import new_id, utcnow
from agent_flight_recorder.openai_agents import instrument_agent
from agent_flight_recorder.replay.engine import ReplayPlan, ReplaySession
from agent_flight_recorder.replay.openai_agents_adapter import run_replay_async
from agents import Agent, RunConfig, Runner, function_tool
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)


class _CollectingTransport:
    """把上报的事件留在内存里：录制侧的行为要被断言，而不是被丢弃。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def send(self, payload: dict[str, Any]) -> IngestResponse:
        request = IngestRequest.model_validate(payload)
        self.events.extend(request.events)
        max_seq = max((event.seq for event in request.events), default=0)
        return IngestResponse(run_id=request.run.id, accepted=len(request.events), max_seq=max_seq)


@function_tool
def flaky(text: str = "x") -> str:
    """总是失败的工具：用来验证失败不会被静默吃掉。"""

    raise RuntimeError("upstream is down")


class _OneToolModel(Model):
    """第一次调用产出一次工具调用，之后收尾。"""

    model_name = "probe-model"

    def __init__(self) -> None:
        self.calls = 0

    async def get_response(
        self,
        system_instructions: Any,
        input: Any,
        model_settings: Any,
        tools: Any,
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        *,
        previous_response_id: Any = None,
        conversation_id: Any = None,
        prompt: Any = None,
    ) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            output: list[Any] = [
                ResponseFunctionToolCall(
                    id="call-item-1",
                    call_id="call_1",
                    type="function_call",
                    name="flaky",
                    arguments='{"text": "x"}',
                )
            ]
        else:
            output = [
                ResponseOutputMessage(
                    id="message-1",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            text="done", type="output_text", annotations=[], logprobs=[]
                        )
                    ],
                )
            ]
        return ModelResponse(
            output=output,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            response_id=None,
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _probe_agent() -> Agent:
    return Agent(name="probe", instructions="probe", model=_OneToolModel(), tools=[flaky])


class _MalformedArgsModel(Model):
    """模型给出的工具参数不是合法 JSON——框架会把这类失败也换成给模型看的文本。"""

    model_name = "probe-model"

    def __init__(self) -> None:
        self.calls = 0

    async def get_response(
        self,
        system_instructions: Any,
        input: Any,
        model_settings: Any,
        tools: Any,
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        *,
        previous_response_id: Any = None,
        conversation_id: Any = None,
        prompt: Any = None,
    ) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            output: list[Any] = [
                ResponseFunctionToolCall(
                    id="call-item-1",
                    call_id="call_1",
                    type="function_call",
                    name="flaky",
                    arguments="{not json",
                )
            ]
        else:
            output = [
                ResponseOutputMessage(
                    id="message-1",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            text="done", type="output_text", annotations=[], logprobs=[]
                        )
                    ],
                )
            ]
        return ModelResponse(
            output=output,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            response_id=None,
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _malformed_agent() -> Agent:
    return Agent(name="probe", instructions="probe", model=_MalformedArgsModel(), tools=[flaky])


def _parent_recording() -> tuple[RunRecord, list[Event]]:
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
            output={"text": "", "tool_calls": [{"id": "call_1", "name": "flaky", "args": {"text": "x"}}]},
            started_at=utcnow(),
        ),
        Event(
            id=new_id(),
            run_id=run_id,
            seq=3,
            type=EventType.TOOL_CALL,
            name="flaky",
            input={"args": {"text": "x"}},
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


def test_a_failing_tool_is_recorded_as_a_failure() -> None:
    """工具失败记成失败：事件里有 error，而不是一句看起来像输出的文本。

    Agent 本身的行为不受影响（模型仍然拿到框架那句话、run 正常结束）：录制不该改变
    被测对象的行为，只该如实记下发生了什么。
    """

    transport = _CollectingTransport()
    recorder = Recorder("probe", enabled=False, transport=transport, flush_interval=0.0)

    recorded = instrument_agent(_probe_agent(), recorder)
    result = asyncio.run(
        Runner.run(recorded, "go", run_config=RunConfig(tracing_disabled=True))
    )
    recorder.close()

    tool_events = [event for event in transport.events if event.type is EventType.TOOL_CALL]
    assert len(tool_events) == 1
    assert tool_events[0].error is not None, "工具失败没有被识别出来"
    assert "upstream is down" in tool_events[0].error.message
    assert result.final_output == "done"


def test_a_failing_live_tool_fails_the_replay() -> None:
    """真实执行的工具失败 = 这次回放失败。

    与 LangGraph 侧一致：那边的工具异常会冒到中间件上，回放同样以失败收场。
    这里如果认不出框架替换过的文本，回放会带着「An error occurred while running the
    tool...」继续跑完并报告成功——那是一个假结论。
    """

    parent, events = _parent_recording()
    session = ReplaySession(
        ReplayPlan(
            parent_run_id=parent.id,
            from_seq=1,
            policy=EffectPolicy(default=EffectMode.LIVE),
        ),
        parent,
        events,
    )
    recorder = Recorder("probe", run_id="replay-run", enabled=False, flush_interval=0.0)

    result = asyncio.run(
        run_replay_async(
            session=session,
            recorder=recorder,
            agent_factory=lambda **_: _probe_agent(),
            tool_side_effects={"flaky": SideEffect.READ},
        )
    )
    recorder.close()

    assert result.status == RunStatus.FAILED.value
    # 工具失败不是「无法判断」：没有成因码，只有失败本身。
    assert result.reason is None
    assert "upstream is down" in (result.error or "")


def test_malformed_tool_arguments_fail_the_replay_instead_of_fabricating_a_result() -> None:
    """模型给出的参数不是合法 JSON：回放必须失败，而不是编一条工具输出继续跑。

    框架默认把这类失败也换成「An error occurred while parsing tool arguments...」交给
    模型继续跑。回放里那样等于凭空造出一条从未录制过的工具输出，因此适配层把这条
    失败通道换成「原样抛出」。改成默认值，这条断言会红。
    """

    parent, events = _parent_recording()
    session = ReplaySession(
        ReplayPlan(
            parent_run_id=parent.id,
            from_seq=1,
            policy=EffectPolicy(default=EffectMode.LIVE),
        ),
        parent,
        events,
    )
    recorder = Recorder("probe", run_id="replay-run", enabled=False, flush_interval=0.0)

    result = asyncio.run(
        run_replay_async(
            session=session,
            recorder=recorder,
            agent_factory=lambda **_: _malformed_agent(),
            tool_side_effects={"flaky": SideEffect.READ},
        )
    )
    recorder.close()

    assert result.status == RunStatus.FAILED.value
    assert "Invalid JSON" in (result.error or "")
