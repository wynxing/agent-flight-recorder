"""离线确定性模型。

存在的理由：让"记录 → 回放 → 对比"这条闭环在没有 API key、没有网络的情况下也能
完整演示，并且每次跑出来的结果完全一致。

它不是一个假模型：它真的接收 messages、真的产出带 tool_calls 的 AIMessage、
真的被 LangGraph 的循环驱动。它只是把"推理"换成了预先写好的剧本。

剧本的选择由 system prompt 里是否出现 GROUNDING_MARKER 决定。因此"改 Prompt 之后
结论改变"在离线模式下是真实发生的行为差异，而不是被硬编码进回放里的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .prompts import GROUNDING_MARKER

SCRIPTED_MODEL_NAME = "afr-scripted-sre-v1"


@dataclass
class ScriptStep:
    text: str = ""
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)


_BUGGY_FINAL = """## 根因结论

checkout-api 的 p99 延迟在 14:02 从约 180ms 抬升到 2.4s。

证据链：
- Prometheus 显示拐点出现在 14:02，此前一直平稳。
- Loki 中出现大量 "redis: connection pool exhausted"。
- Kubernetes 显示 6/6 Pod Ready、0 次重启，容器层面没有异常。
- GitHub 显示 14:01:40 有一次成功的发布 v2.4.1。该发布是一次常规变更，与连接池无关，
  因此不是本次故障的原因。

判定根因：Redis 连接池耗尽。连接池被打满后请求排队，导致 p99 抬升。
建议扩大 Redis 连接池容量。"""

_FIXED_FINAL = """## 根因结论

把指标拐点与部署时间对齐后，因果链是清楚的：

- checkout-api v2.4.1 在 14:01:40 部署完成。
- p99 延迟在 14:02 — 也就是部署完成后约 20 秒 — 出现拐点。
- 该部署把环境变量 REDIS_POOL_SIZE 从 64 改成了 8。

Loki 里的 "redis: connection pool exhausted" 是这次改动造成的结果，不是原因：
连接池被缩小到 8 之后请求开始排队，队列拉长了每次调用的耗时，才把 p99 抬到 2.4s。
把它当成根因会导向一个错误的修复方向。

判定根因：v2.4.1 引入的连接池配置回归。
建议把 REDIS_POOL_SIZE 回滚到 64。"""

# 两套剧本的通知参数刻意完全一致：这样"改 Prompt"带来的差异只体现在推理与结论上，
# 而不是混进工具参数里。回放时才能干净地复现工具步骤、只在结论处分叉。
_NOTIFY_ARGS: dict[str, Any] = {
    "channel": "#sre-oncall",
    "severity": "high",
    "summary": "checkout-api p99 延迟告警，根因调查已完成",
}

BUGGY_SCRIPT: list[ScriptStep] = [
    ScriptStep(
        text="调查计划：先看指标确认拐点，再看日志确认错误形态，然后核对集群状态与最近的部署。",
        tool="prometheus_query",
        args={"query": "histogram_quantile(0.99, checkout_api_latency_seconds)", "window": "30m"},
    ),
    ScriptStep(
        text="指标在 14:02 出现明显拐点，需要确认同期日志。",
        tool="loki_query",
        args={"query": '{app="checkout-api"} |= "error"', "limit": 20},
    ),
    ScriptStep(
        text="日志里 Redis 连接池报错密集出现。先排除容器层面的问题。",
        tool="k8s_describe",
        args={"resource": "deployment/checkout-api", "namespace": "prod"},
    ),
    ScriptStep(
        text="容器状态正常。初步假设是 Redis 连接池耗尽，再核对一次最近的部署记录。",
        tool="github_deployments",
        args={"service": "checkout-api", "limit": 5},
    ),
    ScriptStep(
        text=_BUGGY_FINAL,
        tool="notify_oncall",
        args=_NOTIFY_ARGS,
    ),
    ScriptStep(text="已通知值班。建议先扩容 Redis 连接池，观察 p99 是否回落。"),
]

FIXED_SCRIPT: list[ScriptStep] = [
    ScriptStep(
        text="调查计划：先看指标确认拐点，再看日志确认错误形态，然后核对集群状态与最近的部署，最后把时间线对齐。",
        tool="prometheus_query",
        args={"query": "histogram_quantile(0.99, checkout_api_latency_seconds)", "window": "30m"},
    ),
    ScriptStep(
        text="指标在 14:02 出现明显拐点，需要确认同期日志。",
        tool="loki_query",
        args={"query": '{app="checkout-api"} |= "error"', "limit": 20},
    ),
    ScriptStep(
        text="日志里 Redis 连接池报错密集出现。先排除容器层面的问题。",
        tool="k8s_describe",
        args={"resource": "deployment/checkout-api", "namespace": "prod"},
    ),
    ScriptStep(
        text="容器状态正常。核对最近的部署记录，准备与指标拐点对齐时间。",
        tool="github_deployments",
        args={"service": "checkout-api", "limit": 5},
    ),
    ScriptStep(
        text=_FIXED_FINAL,
        tool="notify_oncall",
        args=_NOTIFY_ARGS,
    ),
    ScriptStep(text="已通知值班。建议回滚 REDIS_POOL_SIZE 到 64，再观察 p99 是否回落。"),
]


class ScriptedChatModel(BaseChatModel):
    """按剧本产出响应的确定性模型。"""

    scripted_model_id: str = SCRIPTED_MODEL_NAME
    trigger_marker: str = GROUNDING_MARKER
    temperature: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "afr-scripted"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.scripted_model_id}

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> "ScriptedChatModel":
        """剧本自己决定调用哪个工具，因此绑定工具只是满足接口约定。

        真实模型要靠 prompt 里的工具描述来决定调用，脚本模型则按剧本走。对录制的
        保真度没有影响：中间件记录的是最终产出的 tool_calls。
        """

        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        script = self._select_script(messages)
        turn = self._turn_index(messages)
        step = script[min(turn, len(script) - 1)]

        tool_calls: list[dict[str, Any]] = []
        if step.tool:
            tool_calls = [
                {
                    "name": step.tool,
                    "args": dict(step.args),
                    "id": f"call_{turn}_{step.tool}",
                    "type": "tool_call",
                }
            ]

        message = AIMessage(
            content=step.text,
            tool_calls=tool_calls,
            response_metadata={"finish_reason": "tool_calls" if tool_calls else "stop"},
            usage_metadata={
                "input_tokens": _estimate_tokens(_render(messages)),
                "output_tokens": _estimate_tokens(step.text),
                "total_tokens": _estimate_tokens(_render(messages)) + _estimate_tokens(step.text),
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _select_script(self, messages: Sequence[BaseMessage]) -> list[ScriptStep]:
        return FIXED_SCRIPT if self.trigger_marker in _system_text(messages) else BUGGY_SCRIPT

    def _turn_index(self, messages: Sequence[BaseMessage]) -> int:
        """已经返回了几次工具结果，就说明剧本走到第几步。"""

        return sum(1 for message in messages if isinstance(message, ToolMessage))


def _system_text(messages: Sequence[BaseMessage]) -> str:
    parts: list[str] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            content = message.content
            parts.append(content if isinstance(content, str) else str(content))
    return chr(10).join(parts)


def _render(messages: Sequence[BaseMessage]) -> str:
    return chr(10).join(str(getattr(message, "content", "")) for message in messages)


def _estimate_tokens(text: str) -> int:
    """粗略估算，仅用于让时间线上的 token 数字有意义。UI 会标注为估算值。"""

    return max(1, len(text) // 4)


__all__ = ["BUGGY_SCRIPT", "FIXED_SCRIPT", "SCRIPTED_MODEL_NAME", "ScriptStep", "ScriptedChatModel"]
