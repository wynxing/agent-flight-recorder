"""构建被测 Agent，并把它注册成平台可以发现、可以回放的 Agent。"""

from __future__ import annotations

import os
from typing import Any, Sequence

from agent_flight_recorder import AgentSpec, Recorder, SideEffect
from agent_flight_recorder.middleware import FlightRecorderMiddleware
from agent_flight_recorder.serialization import text_of
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from .prompts import DEFAULT_SYSTEM_PROMPT
from .scripted_model import SCRIPTED_MODEL_NAME, ScriptedChatModel
from .tools import ALL_TOOLS, TOOL_SIDE_EFFECTS

AGENT_NAME = "checkout-api-sre"
AGENT_VERSION = "0.1.0"

TASK = (
    "告警：checkout-api 的 p99 延迟自 14:02 起从约 180ms 上升到 2.4s，"
    "持续 10 分钟未恢复。请调查根因并通知值班。"
)


def resolve_model(name: str | None) -> Any:
    """把模型标识解析成可用的聊天模型实例。

    默认走离线脚本模型；给了其他名字就走真实的 OpenAI 模型，需要 OPENAI_API_KEY。
    """

    if name is None or name == SCRIPTED_MODEL_NAME:
        return ScriptedChatModel()

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            f"模型 {name!r} 需要 OPENAI_API_KEY 环境变量。"
            f"不配置的话请使用离线模型 {SCRIPTED_MODEL_NAME!r}。"
        )

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的清晰报错
        raise RuntimeError("使用真实模型需要安装 langchain-openai") from exc

    return ChatOpenAI(model=name, temperature=0)


def build_sre_agent(
    *,
    model: str | None = None,
    system_prompt: str | None = None,
    middleware: Sequence[Any] = (),
    checkpointer: Any = None,
) -> Any:
    """回放引擎约定的工厂契约。

    model / system_prompt 为 None 时沿用默认值；非 None 表示回归模式给出了覆盖值。
    """

    return create_agent(
        model=resolve_model(model),
        tools=ALL_TOOLS,
        system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
        middleware=list(middleware),
        checkpointer=checkpointer,
        name=AGENT_NAME,
    )


def run_scenario(
    recorder: Recorder,
    *,
    system_prompt: str | None = None,
    model: str | None = None,
    task: str = TASK,
    recursion_limit: int = 25,
) -> str:
    """完整跑一次告警调查并录制。返回 run_id。"""

    recorder.start(task=task, input={"alert": task, "service": "checkout-api"})

    agent = build_sre_agent(
        model=model,
        system_prompt=system_prompt,
        middleware=[
            FlightRecorderMiddleware(
                recorder,
                tool_side_effects=TOOL_SIDE_EFFECTS,
                default_side_effect=SideEffect.READ,
            )
        ],
    )
    result = agent.invoke(
        {"messages": [HumanMessage(content=task)]},
        config={"recursion_limit": recursion_limit},
    )
    recorder.finish(result=text_of(result["messages"][-1]))
    return recorder.run_id


def agent_spec() -> AgentSpec:
    """平台通过 entry point 发现这个函数，从而知道怎么重建与播种这个 Agent。"""

    return AgentSpec(
        name=AGENT_NAME,
        build=build_sre_agent,
        seed=lambda recorder: run_scenario(recorder),
        description="演示用的 LangGraph SRE 调查 Agent：日志报错是症状，根因在同期部署的配置改动里。",
        version=AGENT_VERSION,
        default_model=SCRIPTED_MODEL_NAME,
        default_system_prompt=DEFAULT_SYSTEM_PROMPT,
        tool_side_effects=TOOL_SIDE_EFFECTS,
    )


__all__ = [
    "AGENT_NAME",
    "AGENT_VERSION",
    "TASK",
    "agent_spec",
    "build_sre_agent",
    "resolve_model",
    "run_scenario",
]

