"""Agent 注册表。

回放需要"重建这个 Agent"的能力，但平台不该硬编码任何一个具体 Agent。
因此约定一个 entry point 组：任何安装进来的 Agent 包都可以把自己注册进来。

    [project.entry-points."afr.agents"]
    checkout-api-sre = "sre_agent.agent:agent_spec"

服务的边界因此是干净的：平台负责记录、回放调度与对比；Agent 的构建方式由 Agent
自己提供。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Callable

from .models import SideEffect
from .recorder import Recorder

ENTRY_POINT_GROUP = "afr.agents"


@dataclass
class AgentSpec:
    """一个可被平台回放的 Agent 的构建说明。"""

    name: str
    build: Callable[..., Any]
    seed: Callable[[Recorder], Any] | None = None
    description: str = ""
    version: str = ""
    default_model: str | None = None
    default_system_prompt: str | None = None
    prompt_presets: dict[str, str] = field(default_factory=dict)
    tool_side_effects: dict[str, SideEffect] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "default_model": self.default_model,
            "default_system_prompt": self.default_system_prompt,
            "prompt_presets": dict(self.prompt_presets),
            "tools": list(self.tool_side_effects),
        }


def load_agent_specs() -> dict[str, AgentSpec]:
    """扫描已安装的 Agent 包。加载失败只跳过并返回诊断信息，不影响平台启动。"""

    specs: dict[str, AgentSpec] = {}
    for entry in _iter_entries():
        try:
            candidate = entry.load()
            spec = candidate() if callable(candidate) and not isinstance(candidate, AgentSpec) else candidate
        except Exception:  # noqa: BLE001 - 单个插件坏了不能拖垮平台
            continue
        if isinstance(spec, AgentSpec):
            specs[spec.name] = spec
    return specs


def resolve_agent_spec(name: str) -> AgentSpec | None:
    return load_agent_specs().get(name)


def _iter_entries():
    try:
        found = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover - 老版本 importlib.metadata
        found = entry_points().get(ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
    return list(found)
