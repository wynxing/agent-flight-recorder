"""Agent 注册表桥接。

平台通过 entry point 发现"怎么重建某个 Agent"，因此不硬编码任何具体 Agent。
未注册的 Agent 依然可以正常录制与查看，只是无法在服务端发起回放。
"""

from __future__ import annotations

from typing import Any

from agent_flight_recorder.registry import ENTRY_POINT_GROUP, AgentSpec, load_agent_specs, resolve_agent_spec

__all__ = [
    "ENTRY_POINT_GROUP",
    "AgentSpec",
    "describe_agents",
    "load_agent_specs",
    "resolve_agent_spec",
]


def describe_agents() -> list[dict[str, Any]]:
    return [spec.describe() for spec in load_agent_specs().values()]

