"""框架适配层的解析表：平台按 Agent 自己声明的 runtime 选择适配层。

这张表存在的原因是一个真实的缺口，而不是「多一个框架多一个文件」：回放核心号称
框架无关，但服务端一直写着

    from agent_flight_recorder.replay.langgraph_adapter import run_replay

第二个框架接入时这才暴露出来——不是核心不认识别的框架，而是**平台只认识一个
适配层**：Agent 注册表里没有任何字段说明这个 Agent 该用哪个适配器重建。于是
「框架无关」在服务平台这一层是假的。

表里存的是**模块路径**而不是模块对象，有两条理由：

1. 框架依赖只在该框架的适配器被真正用到时才导入。SDK 核心仍然只依赖 pydantic
   （见 docs/architecture.md 第 2 节），装了 OpenAI Agents SDK 的用户不必因此拖进
   LangGraph，反过来也一样。
2. 缺依赖时的失败是一件应该被看见的事：落到 AdapterUnavailableError 上并说清
   缺哪个包，而不是在 import 期就把整个 SDK 崩掉。

适配器模块的契约（两个实现都满足，见 docs/architecture.md 第 8 节）：

* ``run_replay(*, session, recorder, agent_factory, tool_side_effects, ...) -> ReplayResult``
* ``agent_factory`` 由适配器自己约定：它负责把 ``model`` / ``system_prompt`` 覆盖值
  交给对应框架的构建函数，因此 AgentSpec.build 不需要知道框架细节。
"""

from __future__ import annotations

import importlib
from types import ModuleType

#: 没声明 runtime 的 Agent 走这里。默认值是 langgraph：
#: 在引入 runtime 字段之前注册的 Agent 全部是 LangGraph Agent，
#: 默认值必须是「原来的行为」，不能让既有 Agent 因为这次改动而改变回放路径。
DEFAULT_ADAPTER = "langgraph"

#: runtime 取值 -> 适配器模块路径。取值同时是 AgentSpec.runtime 的取值集合。
ADAPTER_MODULES: dict[str, str] = {
    DEFAULT_ADAPTER: "agent_flight_recorder.replay.langgraph_adapter",
    "openai-agents": "agent_flight_recorder.replay.openai_agents_adapter",
}


class UnknownAdapterError(ValueError):
    """Agent 声明的 runtime 不在表里。属于配置错误，不是运行时故障。"""


class AdapterUnavailableError(RuntimeError):
    """适配器模块存在但装不上（对应框架的依赖缺失）。"""


def adapter_names() -> tuple[str, ...]:
    """已登记的 runtime 取值。控制台与文档据此列出可选值。"""

    return tuple(ADAPTER_MODULES)


def load_adapter(name: str | None = None) -> ModuleType:
    """按名字取适配器模块。None / 空串按默认适配器处理。"""

    key = name or DEFAULT_ADAPTER
    path = ADAPTER_MODULES.get(key)
    if path is None:
        raise UnknownAdapterError(
            f"未知的 runtime {key!r}。已登记的取值：{', '.join(adapter_names())}。"
            "Agent 包应当在 agent_spec() 里声明自己用哪个框架回放。"
        )
    try:
        return importlib.import_module(path)
    except ImportError as exc:
        raise AdapterUnavailableError(
            f"runtime {key!r} 的适配器 {path} 无法导入：{exc}。"
            "通常是对应框架的依赖没装（例如 pip install 'agent-flight-recorder[openai-agents]'）。"
        ) from exc


__all__ = [
    "ADAPTER_MODULES",
    "DEFAULT_ADAPTER",
    "AdapterUnavailableError",
    "UnknownAdapterError",
    "adapter_names",
    "load_adapter",
]
