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

### 2.3 回放预算

回放请求可以声明硬上限，两个维度各自独立：

```json
{ "from_seq": 15, "preset": "regress", "budget": { "max_cost_usd": 0.5, "max_model_calls": 20 } }
```

两个都不给就是**不设上限**，此时回放行为与没有这套能力时一致。上限在 **SDK 层**生效（回放引擎本来就能脱离平台独立运行）：达到任一上限时引擎在**步边界**停止，不再发起新的模型调用；已经发出的那次调用允许跑完并如实记账。

触顶属于 `inconclusive`，成因码是 `budget_exceeded`（见 [回放语义](replay-semantics.md) 第 9 节），Run 状态是 `aborted` 而不是 `failed`。

提交前可以先预估：

```
POST /v1/runs/{id}/replay/estimate   {from_seq, preset | policy, model?}
                                    -> {parent_run_id, from_seq, model_calls, cost_usd, cost_is_estimate, detail}
```

预估**不调用任何模型**，也不创建 Run。父 Run 缺少可用 token 记录、或模型不在本地价格表内时，`cost_usd` 为 `null`（无法预估），而不是一个编造的数字。

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

`cases.last_cause` 的契约是：**结论不是 passed / failed（即 inconclusive 或 error）时非空，
passed / failed 时为 `null`**。error 也会带上成因，它的 `code` 说明为什么这次执行没有可信结论；
只有结论没有成因，用户就无从知道该去看录制质量、副作用策略还是执行本身。

声明了上限的回放，`metadata.afr_replay.budget` 给出「已用 / 上限 / 是否触顶」的对照：

| 字段 | 含义 |
| --- | --- |
| `max_cost_usd` / `max_model_calls` | 声明的上限；`null` 表示这一维不参与判定 |
| `model_calls_used` | 已用：真实发生的模型调用次数 |
| `cost_used_usd` | 已用成本（估算）；`null` = **未知**，不是 0 |
| `cost_unknown` | 是否存在无法定价的真实调用 |
| `exceeded` / `stopped_by` | 是否因触顶而停止、停在哪一维（`model_calls` / `cost`） |
| `detail` | 一句人话说明 |

没有声明上限的回放不带这个字段——「没声明」不等于「上限为 0」，也不该凭空多出一个「上限：无」的账目。

pi 使用 `metadata.runtime="pi"` 与 `labels.runtime="pi"`，由本地运行器回放并产生新的 parent_run_id。

## 7. 批量套件

批量运行（一次跑一批用例 × 一组条件）不改变上面的上报协议，它的契约只在服务端与控制台之间。
六条约定是硬约束，不是实现细节：

1. **条件随结果记录。** 每个格子都带 `condition = {prompt, model, system_prompt, preset}`，
   `prompt` / `model` 由请求给出，`system_prompt` 是执行当时解析出来的 Prompt 正文，
   `preset` 是实际用到的回放模式。同一条用例在不同条件下的结论因此分别可辨认。
   给了 `prompt` 或 `model` 就按回归模式执行：复现模式完全使用录制结果，换 Prompt 或换模型
   不会产生任何影响，那样的结论看起来正常、其实什么都没验证。
2. **不合成总分。** 汇总只按条件分组给出四态计数与可判断率：
   `determinable = passed + failed`，分母是该条件自己的 `total`。
   任何跨条件的合计分数（百分制、加权分、总通过率）都不提供，控制台也不自行计算（见 PRD 6.5）。
3. **「还没跑」不等于「拿不到结论」。** 每个条件有三个互斥且穷尽的桶：
   `total = determinable + undecided + unfinished`。
   `undecided` 只包括**跑过了、但没有可信结论**的格子（`inconclusive + error`）；
   `unfinished` 是**还没跑完**的格子（`pending + running + not_started`）。
   未完成的格子从未执行过、因此没有任何结论，把它算进 `undecided` 等于对一个从未运行过的
   格子下「跑过了、但拿不到」这个断言。控制台据此分两个时刻说话：还有格子未完成时只陈述
   「已完成 / 未完成」，整批完成后才说「N 条中 M 条拿不到结论」（M 即 `undecided`）。
   同一张卡片上每个短语只对应一个数：已完成 = `completed`、未完成 = `unfinished`、
   拿不到结论 = `undecided`，通过 / 未通过 / 无法判断 / 执行出错 分别等于同名的计数。
   `unfinished` 里的 `not_started` 单独再给一个数：它说的是「不会再轮到」，
   与「还没轮到」不是一回事（见第 5 条与 [回放语义](replay-semantics.md) 第 10 节）。
4. **inconclusive 不等于 failed。** 聚合沿用 `code` + `detail` 的成因分类：
   `inconclusive`、`error`、`failed` 三态分列，每个格子的成因逐条可见。
   成因只属于已经跑完的两类：`inconclusive` 与 `error` 的格子必定带成因，
   未完成的格子必定没有成因。
5. **整批可以声明上限，没跑成的格子如实标注。** 提交时可以声明这一批最多花多少
   （`budget`，与单次回放同一套 `ReplayBudget` 语义）；不声明 = 与没有这套能力时
   逐字一致。到点之后**不再启动新格子**，已启动的格子跑完并如实记账，没轮到的格子状态是
   `not_started`（呈现为「未启动（因批次预算用尽）」），它没有结论、没有成因，
   因此不计入 `failed`、也不计入 `undecided`，而是归在 `unfinished` 一侧。
6. **结论归属于提交那一刻的用例集版本。** 提交时把这一批用到的用例定义固化成一份可寻址的版本
   （`cs1:<内容摘要>`，见 architecture.md 第 4.5 节），套件与每个格子都带着它，按标识能反查
   **当时**那一份定义。标识是内容决定的：只改一条用例的断言或起点就会得到另一个版本，同一份定义
   跑两遍仍是同一版。**每格执行的是提交那一刻冻结的那一份快照**，执行期不再读用例行，因此
   批次执行期间用例被编辑不会造成「前半批按旧断言、后半批按新断言，却报成一个数」。
   `case_set.drift` 如实提示「这些用例自本批之后被改动过 / 已经找不到」，它只说用例页的内容
   变了，**不改变本批任何一条结论的归属**。历史批次（版本化之前创建的）读出来是 `null`，
   即「无版本记录」——不按当前用例反推一个版本号补上去。

```
POST /v1/suites            {case_ids: [..] | all_cases: true, conditions: [{prompt, model}],
                            budget?: {max_cost_usd?, max_model_calls?}}
                           -> {suite_id, status, total, conditions, case_set_version}
                              # 立即返回，执行在后台；归属在提交那一刻就已确定
POST /v1/suites/estimate   {case_ids [..] | all_cases: true, conditions, budget?}
                           -> {cells, model_calls, cost_usd, cost_is_estimate, detail}
                              # 只读：不调用模型、不创建批次；成本给不出来时是 null + 原因
GET  /v1/suites            -> {suites: [{id, status, total, completed, counts, errors,
                                          case_set_version, ...}]}
GET  /v1/suites/{id}       -> {id, status, total, completed, counts, errors,
                               budget: {max_cost_usd, max_model_calls, model_calls_used,
                                        cost_used_usd, cost_is_estimate, exceeded, stopped_by,
                                        cost_unknown, not_started, detail} | null,
                               case_set: {id, canonicalization, case_count, recorded,
                                          recorded_at, drift: {changed, missing, unchanged}} | null,
                               groups: [{condition, label, total, completed, counts,
                                         determinable, undecided, unfinished, not_started,
                                         determinable_rate, errors,
                                         items: [{case_id, case_set_version, condition, status,
                                                  run_id, results, cause}]}]}
GET  /v1/case-set-versions/{id}
                           -> {id, canonicalization, case_count, recorded_at,
                               cases: [{case_id, digest, definition: {source_run_id, from_seq,
                                        to_seq, assertions, effect_policy}}]}
                              # 某一版固化的定义：用例后来被改成什么样都不影响这里的内容
PATCH /v1/cases/{id}       -> CaseItem   # 改用例定义（只改给出的字段）；已落库的结论一个字不动
```

`label` 只描述这个条件**实际覆盖了**什么：没写模型就写「沿用用例自身模型」，不替它起一个
「默认模型」之类的名字——服务端担保不了那个默认值。

格子的状态取值是 `pending / running / not_started / passed / failed / inconclusive / error`；
前三个是未完成态，不是结论，因此永远不会被并进 `failed`（也不会被并进 `undecided`）。
其中 `not_started` 是「未启动（因批次预算用尽）」：格子从未被执行过，既不是「没通过」，
也不是「跑过了但拿不到结论」。用例侧的 `last_condition` 记录「最近一次结论是在什么条件下
得出的」；单条运行不带条件时为 `null`（该入口允许直接传 Prompt 正文覆盖而不带版本名，
凭空补一个条件名会把一次真实覆盖描述成「什么都没变」）。

**用例行的「最近一次结论」是整条写下去的。** 记录结论的那次写入，把 `last_status`、
`last_run_id`、`last_run_at`、`last_results`、`last_cause`、`last_condition`、
`last_definition_digest` 七个字段放进
**同一条 UPDATE**；开始一次新执行的那次写入同样整条落下（它负责的是其中除了成因的那几个，
定义摘要也在其中：那一次还没有结论，也就还没有「哪一版定义得出的结论」这句话）。
谁最后提交，这一整条记录就整个来自谁。

其中 `last_definition_digest` 说的是「最近一次结论是在**哪一版定义**下得出的」。它与用例当前的
`definition_digest` 不同时，说明这条结论不描述现在这份定义（用例被改过）；存量行没有这一列，
读出来是 `null`，也就是「没有记录」——控制台在 `null` 时什么都不说，不拿「没记录」冒充「一致」。

这不是实现细节，而是用例页那句「最近一次结论」的前提：一条用例在同一批里会被多个条件
并发执行，每个格子都要写这一行。若按列合并（先读出行对象、改属性、只提交与读到的快照
相比有变化的列），后提交的那一方会把自己没改动的列留在对方写下的值上，于是出现
「`last_run_id` 指向 default 格子、`last_condition` 却是 grounded」这种自相矛盾的记录——
用户看到的那句话就同时属于两次执行，谁也说不清它是什么条件下的结论。
