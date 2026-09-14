"""离线确定性模型（Agents SDK 版）。

存在的理由与 LangGraph 版相同：让「记录 → 回放 → 对比」这条闭环在没有 API key、
没有网络的情况下也能完整演示，并且每次跑出来完全一致。

它不是假模型，而是一个**真的 Model 实现**：真的接收 input 条目、真的产出
ResponseFunctionToolCall、真的被 Runner 的循环驱动。它只是把「推理」换成预先写好的剧本。
剧本的选择由 system_instructions 里是否出现 GROUNDING_MARKER 决定，因此「改 Prompt 之后
结论改变」在离线模式下是真实发生的行为差异，而不是硬编码进回放的。

接入点与 LangChain 版完全不同：那边要继承 BaseChatModel 并实现 _generate，
这边实现 Agents SDK 的 Model 协议（get_response）。判定「剧本走到第几步」的方式也不同：
那边数 ToolMessage，这边数 Responses API 的 function_call_output 条目。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Sequence

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from .prompts import GROUNDING_MARKER

SCRIPTED_MODEL_NAME = "afr-scripted-sre-v1"

#: function_call_output 条目类型：数它就知道剧本走到第几步。
ITEM_FUNCTION_CALL_OUTPUT = "function_call_output"
NEWLINE = chr(10)


class ScriptStep:
    """剧本里的一步：一句话 + 可选的一次工具调用。"""

    __slots__ = ("text", "tool", "args")

    def __init__(self, text: str = "", tool: str | None = None, args: dict[str, Any] | None = None):
        self.text = text
        self.tool = tool
        self.args = dict(args or {})


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
- p99 延迟在 14:02 出现拐点，也就是部署完成后约 20 秒。
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


class ScriptedSreModel(Model):
    """按剧本产出响应的确定性模型（实现 Agents SDK 的 Model 协议）。"""

    #: 让 model_identifier 取到稳定标识：录制里的模型名两侧必须一致，
    #: 否则「同一份录制在两个框架上对比」会因为模型名不同而看起来有差异。
    model_name = SCRIPTED_MODEL_NAME

    def __init__(self, *, trigger_marker: str = GROUNDING_MARKER) -> None:
        self.trigger_marker = trigger_marker

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
        script = self._select_script(system_instructions)
        turn = self._turn_index(input)
        step = script[min(turn, len(script) - 1)]

        output: list[Any] = []
        if step.text:
            output.append(_assistant_message(step.text, index=turn))
        if step.tool:
            output.append(_function_call(step.tool, step.args, turn=turn))

        input_tokens = _estimate_tokens(_render(input))
        output_tokens = _estimate_tokens(step.text)
        return ModelResponse(
            output=output,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            response_id=None,
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """流式留给专门轮次：本轮只验证非流式的录制与复现闭环。"""

        raise NotImplementedError(
            "离线剧本模型不实现 stream_response：本轮验证的是 Runner.run 这条非流式路径。"
        )

    def _select_script(self, system_instructions: Any) -> list[ScriptStep]:
        text = system_instructions if isinstance(system_instructions, str) else ""
        return FIXED_SCRIPT if self.trigger_marker in text else BUGGY_SCRIPT

    def _turn_index(self, input: Any) -> int:
        """已经返回了几次工具结果，就说明剧本走到第几步。"""

        return sum(
            1
            for item in _items(input)
            if _item_type(item) == ITEM_FUNCTION_CALL_OUTPUT
        )


def _items(input: Any) -> Sequence[Any]:
    if isinstance(input, list):
        return input
    return []


def _item_type(item: Any) -> Any:
    if isinstance(item, dict):
        return item.get("type")
    return getattr(item, "type", None)


def _assistant_message(text: str, *, index: int) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id=f"scripted-message-{index}",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(text=text, type="output_text", annotations=[], logprobs=[])],
    )


def _function_call(name: str, args: dict[str, Any], *, turn: int) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id=f"scripted-call-{turn}",
        call_id=f"call_{turn}_{name}",
        type="function_call",
        name=name,
        arguments=json.dumps(args, ensure_ascii=False),
    )


def _render(input: Any) -> str:
    parts: list[str] = []
    for item in _items(input):
        if isinstance(item, dict):
            parts.append(json.dumps(item, ensure_ascii=False, default=str))
        else:
            parts.append(str(item))
    return NEWLINE.join(parts)


def _estimate_tokens(text: str) -> int:
    """粗略估算，仅用于让时间线上的 token 数字有意义（与 LangChain 侧同一条口径）。"""

    return max(1, len(text) // 4)


__all__ = [
    "BUGGY_SCRIPT",
    "FIXED_SCRIPT",
    "SCRIPTED_MODEL_NAME",
    "ScriptStep",
    "ScriptedSreModel",
]
