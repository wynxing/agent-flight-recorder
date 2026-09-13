# agent-flight-recorder (Python SDK)

记录、回放与调试 AI Agent 的 Python SDK。

- 采集入口是一个 LangChain / LangGraph `AgentMiddleware`，加一行即可录制。
- 录制链路永不向上抛异常：录制失败不允许打断被观测的 Agent。
- 回放引擎位于 SDK 内，可以脱离服务端独立调用。

协议见 `docs/protocol.md`，回放语义见 `docs/replay-semantics.md`。

