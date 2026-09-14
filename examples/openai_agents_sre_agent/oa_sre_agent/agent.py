"""构建被测 Agent，并把它注册成平台可以发现、可以回放的 Agent。

与 LangGraph 版的差别不止是 API 名字：

* 构建 —— Agent(name=..., instructions=..., model=..., tools=[...])；
* 运行 —— Runner.run 是异步的，因此这里给出 run_scenario_async 与同步外壳；
* 录制 —— 没有中间件可挂，用 instrument_agent 复制一份 Agent 并替换它的
  model 与 tools 两个字段（见 sdk/agent_flight_recorder/openai_agents.py）；
* 回放 —— AgentSpec.runtime 声明成 "openai-agents"，平台据此选适配层，
  而不是假设所有 Agent 都在 LangGraph 上。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from agent_flight_recorder import AgentSpec, Recorder, SeedCase, SideEffect
from agent_flight_recorder.openai_agents import instrument_agent
from agent_flight_recorder.replay.openai_agents_adapter import final_text
from agents import Agent, RunConfig, Runner

from .prompts import DEFAULT_SYSTEM_PROMPT, GROUNDED_SYSTEM_PROMPT
from .scripted_model import SCRIPTED_MODEL_NAME, ScriptedSreModel
from .tools import ALL_TOOLS, SERVICE, TOOL_SIDE_EFFECTS

AGENT_NAME = "checkout-api-sre-agents-sdk"
AGENT_VERSION = "0.1.0"
#: 回放适配层的取值（见 sdk/agent_flight_recorder/replay/adapters.py）。
RUNTIME = "openai-agents"
#: 循环上限。真正的预算不在这里，而在 ReplaySession 的账本里（与框架无关）。
MAX_TURNS = 20

TASK = (
    "告警：checkout-api 的 p99 延迟自 14:02 起从约 180ms 上升到 2.4s，"
    "持续 10 分钟未恢复。请调查根因并通知值班。"
)


def resolve_model(name: str | None) -> Any:
    """把模型标识解析成 Model 实例。

    默认走离线剧本模型；给了其他名字就走真实的 OpenAI 模型，需要 OPENAI_API_KEY。
    真实模型这条路本轮没有跑过（见 PR 里的未验证面），保留它是为了让「换成真模型」
    是一次参数改动，而不是一次改写。
    """

    if name is None or name == SCRIPTED_MODEL_NAME:
        return ScriptedSreModel()

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            f"模型 {name!r} 需要 OPENAI_API_KEY 环境变量。"
            f"不配置的话请使用离线模型 {SCRIPTED_MODEL_NAME!r}。"
        )

    from agents import AsyncOpenAI, OpenAIChatCompletionsModel

    return OpenAIChatCompletionsModel(model=name, openai_client=AsyncOpenAI())


def build_sre_agent(*, model: str | None = None, system_prompt: str | None = None) -> Agent:
    """回放引擎约定的工厂契约。

    model / system_prompt 为 None 时沿用默认值；非 None 表示回归模式给出的覆盖值。
    与 LangGraph 侧不同，这里不接收 middleware：接管点是 Agent 自己的 model 与
    tools 字段，回放适配器直接替换它们。
    """

    return Agent(
        name=AGENT_NAME,
        instructions=system_prompt or DEFAULT_SYSTEM_PROMPT,
        model=resolve_model(model),
        tools=list(ALL_TOOLS),
    )


async def run_scenario_async(
    recorder: Recorder,
    *,
    system_prompt: str | None = None,
    model: str | None = None,
    task: str = TASK,
) -> str:
    """完整跑一次告警调查并录制。返回 run_id。"""

    recorder.run.metadata["afr_replay_context"] = "task_only"
    recorder.start(task=task, input={"alert": task, "service": SERVICE})

    agent = build_sre_agent(model=model, system_prompt=system_prompt)
    recorded = instrument_agent(
        agent,
        recorder,
        tool_side_effects=TOOL_SIDE_EFFECTS,
        default_side_effect=SideEffect.READ,
    )
    result = await Runner.run(
        recorded,
        task,
        max_turns=MAX_TURNS,
        # 全程离线：关掉 tracing，既不联网也不需要凭证。
        run_config=RunConfig(tracing_disabled=True),
    )
    recorder.finish(result=final_text(result))
    return recorder.run_id


def run_scenario(
    recorder: Recorder,
    *,
    system_prompt: str | None = None,
    model: str | None = None,
    task: str = TASK,
) -> str:
    """同步外壳：平台播种与示例脚本都用它。"""

    return asyncio.run(
        run_scenario_async(recorder, system_prompt=system_prompt, model=model, task=task)
    )


def agent_spec() -> AgentSpec:
    """平台通过 entry point 发现这个函数，从而知道怎么重建与播种这个 Agent。"""

    return AgentSpec(
        name=AGENT_NAME,
        build=build_sre_agent,
        seed=lambda recorder: run_scenario(recorder),
        description=(
            "演示用的 OpenAI Agents SDK SRE 调查 Agent：与 LangGraph 版共用同一份场景与"
            "台词，用来验证同一个回放内核在第二个框架上给出同一结论。"
        ),
        version=AGENT_VERSION,
        default_model=SCRIPTED_MODEL_NAME,
        default_system_prompt=DEFAULT_SYSTEM_PROMPT,
        prompt_presets={"default": DEFAULT_SYSTEM_PROMPT, "grounded": GROUNDED_SYSTEM_PROMPT},
        tool_side_effects=TOOL_SIDE_EFFECTS,
        runtime=RUNTIME,
        # 与 LangGraph 侧对称的一条开局即失败的用例：默认 Prompt 下把症状当根因，
        # 切到 grounded 才会通过。
        seed_cases=[
            SeedCase(
                name="根因必须指向 REDIS_POOL_SIZE 配置回归（Agents SDK）",
                description=(
                    "同一个场景在第二个框架上的同一条断言：默认 Prompt 会失败，"
                    "换 grounded Prompt 才通过——红绿翻转在第二个框架上同样成立。"
                ),
                from_seq=15,
                assertions=[{"type": "final_output_contains", "value": "REDIS_POOL_SIZE"}],
            )
        ],
    )


__all__ = [
    "AGENT_NAME",
    "AGENT_VERSION",
    "MAX_TURNS",
    "RUNTIME",
    "TASK",
    "agent_spec",
    "build_sre_agent",
    "resolve_model",
    "run_scenario",
    "run_scenario_async",
]
