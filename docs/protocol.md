# Agent Flight Recorder 上报协议 v1

本文档描述实验阶段 SDK 与平台之间的数据契约。协议版本为 `1`，通过 HTTP 传输，载荷为 JSON。

pi TypeScript 运行器复用当前协议；通用 TS SDK 不在本轮范围内。新增适配器仍需验证回放上下文，不以 HTTP 兼容性代替语义兼容性。

## 1. 传输

| 项 | 值 |
| --- | --- |
| 端点 | `POST /v1/ingest` |
| Content-Type | `application/json` |
| 认证 | 无（本地单用户） |
| 幂等键 | `(run.id, event.seq)` |

SDK 以批量方式上报。一次请求就是一次完整的 `IngestRequest`：

```json
{
  "protocol_version": 1,
  "sdk": { "name": "agent-flight-recorder-python", "version": "0.1.0" },
  "run": { "...Run 头..." },
  "events": [ { "...Event..." } ]
}
```

### 1.1 分片与续传

`run` 头在每个批次里都必须完整携带，服务端按 `run.id` 做 upsert。因此：

- 第一批可以只带 `run_started` 事件。
- 后续批次继续追加事件，服务端按 `seq` 去重，重复提交不会产生重复行。
- 批次可重发：网络失败后 SDK 重试整批是安全的。
- `seq` 在单个 Run 内必须严格单调递增，从 1 开始。

服务端返回：

```json
{
  "run_id": "…",
  "accepted": 12,
  "duplicates": 0,
  "rejected": [],
  "max_seq": 12
}
```

### 1.2 失败语义

SDK 侧的录制链路必须**永不向上抛异常**。任何传输、序列化或服务端错误都只能被本地记录（丢弃计数 + 可选回调），绝不打断或阻塞被观测的 Agent。短生命周期脚本必须显式调用 `flush()`，否则缓冲区内的事件会随进程退出丢失。

## 2. Run

Run 表示一次完整的 Agent 执行。回放产生的是**新的 Run**，不是对旧 Run 的修改。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 客户端生成的稳定 ID |
| `agent_name` | string | 是 | |
| `agent_version` | string \| null | 否 | 代码/Prompt 版本标识 |
| `model` | string \| null | 否 | 主要模型标识 |
| `status` | enum | 是 | `running` \| `succeeded` \| `failed` \| `aborted` |
| `parent_run_id` | string \| null | 否 | 回放时指向被回放的 Run |
| `replay_from_seq` | int \| null | 否 | 从第几步开始分叉 |
| `effect_policy` | EffectPolicy \| null | 否 | 回放策略；非回放 Run 为 null |
| `prompt_version` | string \| null | 否 | |
| `labels` | object[str,str] | 否 | 自由标签，用于筛选与标注 |
| `metadata` | object | 否 | 自由元数据 |
| `started_at` | ISO8601 | 是 | |
| `ended_at` | ISO8601 \| null | 否 | |
| `summary` | object | 否 | 由服务端计算：token、成本、耗时、步数 |

### 2.1 EffectPolicy

回放的核心语义。三值枚举：

| 值 | 含义 |
| --- | --- |
| `recorded` | 使用父 Run 录制的结果，不执行真实调用 |
| `live` | 真实执行（真实调用模型或工具） |
| `dry_run` | 拦截副作用，返回"本应做什么"的合成结果 |

```json
{
  "default": "recorded",
  "by_kind": { "model_call": "live", "tool_call": "recorded" },
  "by_seq": { "7": "live" },
  "allow_side_effect_execution": false
}
```

**优先级固定为：`by_seq[seq]` > `by_kind[event_kind]` > `default`，默认值为 `recorded`。**

两个 UI 预设直接映射到这套配置：

- **复现模式（Reproduce）**：`default = recorded`。目标是验证"从第几步开始偏离"。
- **回归模式（Regress）**：`by_kind.model_call = live`、`by_kind.tool_call = recorded`。目标是验证"改完到底有没有变好"。

### 2.2 副作用闸门

每个 Tool 声明副作用等级：

| 等级 | 含义 | 回放默认行为 |
| --- | --- | --- |
| `read` | 只读查询 | 可 `live`、`recorded` 或 `dry_run` |
| `write` | 修改内部状态 | 强制 `dry_run` |
| `external` | 触发对外动作（发消息、开工单、部署） | 强制 `dry_run` |

`write` / `external` 在回放中默认被强制降级为 `dry_run`，仅在该步请求 live 时；recorded 继续读取录制结果。要真实执行必须同时满足：`allow_side_effect_execution = true` **且** 该步的策略显式为 `live`。真实执行时引擎必须写入一条高可见度的告警事件（`attributes.severity = "warning"`），使这次副作用在时间线上无法被忽略。

## 3. Event

Event 是 append-only 的。写入后不再修改。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 稳定 ID |
| `run_id` | string | |
| `seq` | int | Run 内严格单调，从 1 开始 |
| `type` | enum | 见下表 |
| `parent_seq` | int \| null | 因果归属 |
| `source_seq` | int \| null | 回放时指向父 Run 中被复现的步骤 |
| `name` | string \| null | 工具名 / 模型名 / 节点名 |
| `started_at` | ISO8601 | |
| `ended_at` | ISO8601 \| null | |
| `duration_ms` | float \| null | |
| `input` | object \| null | 见事件类型定义 |
| `output` | object \| null | 见事件类型定义 |
| `error` | object \| null | `{type, message, stack?}` |
| `tokens` | object \| null | `{input, output, total}` |
| `cost_usd` | float \| null | 估算值 |
| `side_effect` | enum \| null | `read` \| `write` \| `external` |
| `effect_source` | enum \| null | `live` \| `recorded` \| `dry_run` \| `blocked` |
| `redactions` | string[] | 命中的脱敏规则名 |
| `attributes` | object | 自由扩展位 |

### 3.1 事件类型

| 类型 | input | output |
| --- | --- | --- |
| `run_started` | `{task, input}` | null |
| `model_call` | `{messages, tools, model, params}` | `{text, tool_calls, finish_reason, usage}` |
| `tool_call` | `{args}` | `{result}` |
| `state_snapshot` | null | `{state}` |
| `error` | object \| null | null（错误在 `error` 字段） |
| `run_finished` | null | `{result, status}` |

`attributes` 中的保留键：

| 键 | 说明 |
| --- | --- |
| `checkpoint_ref` | LangGraph 等框架的原生 checkpoint 标识，供需要时用框架能力恢复 |
| `node` | 图节点名 |
| `severity` | `info` \| `warning`；副作用真实执行时置为 `warning` |
| `synthetic` | bool，合成结果（dry_run / recorded）标记 |

## 4. Checkpoint

本协议**不定义独立的状态快照格式**。Checkpoint 是事件日志在步边界的投影：`state_snapshot` 事件在每一步边界的落点天然构成可回放位点。

需要框架原生状态时，把标识写进 `attributes.checkpoint_ref`，由框架自身能力负责恢复；平台不复制框架的状态机。

## 5. 脱敏

入库前执行最小脱敏：命中规则的值被替换为 `[REDACTED:<rule>]`，命中规则名记入事件与 Run 的 `redactions`。默认规则覆盖常见凭证形态（`Authorization` 头、`sk-*`、`ghp_*`、`AKIA*`、Bearer token、看起来像私钥的块）。

脱敏在**服务端入库时**执行，因此 SDK 本地缓冲里仍是明文；生产部署应把 SDK 的 endpoint 视为可信边界。

## 6. 版本策略

- `protocol_version` 为整数，破坏性变更才递增。
- 服务端接受 `protocol_version <= 1`，对未知字段采取忽略策略（前向兼容）。
- 新增可选字段不递增版本号。


## 6. 实验性完整性元数据

Run 状态枚举不变。`metadata.afr_recording.complete=false` 表示 SDK 已知录制丢失；回放还会检查事件边界、序列缺口和脱敏。
`metadata.afr_replay` 可包含 `complete`、`reason`、`cause` 和 `verdict`。用例结论为 passed / failed / inconclusive / error，运行 succeeded 不能单独证明评测通过。

`metadata.afr_replay.cause` 是「为什么无法判断」的结构化成因，恒为 `{code, detail}`：`code` 是跨层共享的闭集取值（见 [回放语义](replay-semantics.md) 第 8 节），`detail` 是给人看的说明。`reason` 为兼容字段：新写入时与 `cause` 内容一致，历史数据里可能是自由字符串，服务端读取时归一化。用例侧的同一结构在 `cases.last_cause`；pi 上报时把结构直接放进 `afr_replay.reason`。
pi 使用 `metadata.runtime="pi"` 与 `labels.runtime="pi"`，由本地运行器回放并产生新的 parent_run_id。
