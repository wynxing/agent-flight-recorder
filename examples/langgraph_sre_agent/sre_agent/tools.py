"""自带 mock 的 SRE 工具集。

全部离线、确定性、无副作用（除了 notify_oncall 这个被显式标为 external 的工具）。
这样演示不依赖任何真实监控系统，也不会因为外部状态漂移而不可复现。
"""

from __future__ import annotations

import json
from typing import Any

from agent_flight_recorder import SideEffect, afr_tool
from langchain_core.tools import tool

SERVICE = "checkout-api"
INCIDENT_WINDOW = "2026-09-13T13:45:00Z/2026-09-13T14:15:00Z"
DEPLOY_TIME = "2026-09-13T14:01:40Z"
INFLECTION_TIME = "2026-09-13T14:02:00Z"


@tool
@afr_tool(SideEffect.READ)
def prometheus_query(query: str, window: str = "30m") -> str:
    """查询 Prometheus 指标。参数是 PromQL 查询与时间窗口。"""

    payload = {
        "query": query,
        "window": window,
        "series": [
            {"t": "2026-09-13T13:58:00Z", "v": 0.18},
            {"t": "2026-09-13T14:00:00Z", "v": 0.19},
            {"t": INFLECTION_TIME, "v": 0.42},
            {"t": "2026-09-13T14:04:00Z", "v": 1.36},
            {"t": "2026-09-13T14:08:00Z", "v": 2.41},
            {"t": "2026-09-13T14:12:00Z", "v": 2.38},
        ],
        "unit": "seconds",
        "note": "p99 延迟在 14:02 出现拐点",
    }
    return json.dumps(payload, ensure_ascii=False)


@tool
@afr_tool(SideEffect.READ)
def loki_query(query: str, limit: int = 20) -> str:
    """查询 Loki 日志。参数是 LogQL 查询与条数上限。"""

    lines = [
        {"t": "2026-09-13T14:02:31Z", "level": "error", "msg": "redis: connection pool exhausted"},
        {"t": "2026-09-13T14:03:02Z", "level": "error", "msg": "redis: connection pool exhausted"},
        {"t": "2026-09-13T14:03:44Z", "level": "warn", "msg": "checkout handler latency 1840ms"},
        {"t": "2026-09-13T14:04:12Z", "level": "error", "msg": "redis: connection pool exhausted"},
    ]
    return json.dumps({"query": query, "limit": limit, "lines": lines}, ensure_ascii=False)


@tool
@afr_tool(SideEffect.READ)
def k8s_describe(resource: str, namespace: str = "prod") -> str:
    """查看 Kubernetes 资源状态。"""

    payload = {
        "resource": resource,
        "namespace": namespace,
        "replicas": {"desired": 6, "ready": 6, "restarts": 0},
        "conditions": ["PodScheduled=True", "Ready=True"],
        "note": "容器层面没有异常信号",
    }
    return json.dumps(payload, ensure_ascii=False)


@tool
@afr_tool(SideEffect.READ)
def github_deployments(service: str, limit: int = 5) -> str:
    """查询最近的部署记录，包含该次部署改动的配置项。"""

    payload = {
        "service": service,
        "deployments": [
            {
                "version": "v2.4.1",
                "deployed_at": DEPLOY_TIME,
                "status": "success",
                "changes": [
                    {
                        "kind": "env",
                        "name": "REDIS_POOL_SIZE",
                        "before": "64",
                        "after": "8",
                    }
                ],
            },
            {
                "version": "v2.4.0",
                "deployed_at": "2026-09-12T09:30:00Z",
                "status": "success",
                "changes": [],
            },
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


@tool
@afr_tool(SideEffect.EXTERNAL)
def notify_oncall(channel: str, severity: str, summary: str) -> str:
    """通知值班。会真实触达外部系统，因此在回放中默认被拦截。"""

    payload: dict[str, Any] = {
        "channel": channel,
        "severity": severity,
        "summary": summary,
        "delivered": True,
    }
    return json.dumps(payload, ensure_ascii=False)


ALL_TOOLS = [prometheus_query, loki_query, k8s_describe, github_deployments, notify_oncall]

TOOL_SIDE_EFFECTS: dict[str, SideEffect] = {
    "prometheus_query": SideEffect.READ,
    "loki_query": SideEffect.READ,
    "k8s_describe": SideEffect.READ,
    "github_deployments": SideEffect.READ,
    "notify_oncall": SideEffect.EXTERNAL,
}

__all__ = [
    "ALL_TOOLS",
    "DEPLOY_TIME",
    "INCIDENT_WINDOW",
    "INFLECTION_TIME",
    "SERVICE",
    "TOOL_SIDE_EFFECTS",
    "github_deployments",
    "k8s_describe",
    "loki_query",
    "notify_oncall",
    "prometheus_query",
]

