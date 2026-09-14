# 回放语义

本文档定义 Agent Flight Recorder 最核心的契约：一次回放到底发生了什么、能保证什么、不能保证什么。

## 1. 两种语义，不要混为一谈

"Replay" 在工程上是两件不同的事。混在一起会导致成本、可信度和 UI 全部失焦，因此本平台显式拆开。

### 复现（Reproduce）

目标：**把那次事故一模一样地重放一遍**，回答"它从第几步开始偏离"。

配置：全部步骤 `recorded`。模型输出和工具结果都取自父 Run 的录制，不产生任何真实调用。

这条路径必须是确定性的：除了时间戳、ID 与 effect_source（它本身就是用来标注结果
来源的字段），事件序列与父 Run 的行为内容逐字段一致。

这个保证有明确边界，不满足时回放直接判为**无法判断**（而不是继续跑出一个看似合理的结论）：

- 事件边界或 `seq` 序列不完整；
- 父 Run 或任一事件发生过脱敏；
- SDK 已知发生录制丢失（`metadata.afr_recording.complete = false`）；
- 模型输入的消息条数被截断（录制只保留最近 80 条，`message_count` 大于实际保存的 messages 条数）；
- 父 Run 记录了初始 input，但回放既没有把完整 `initial_state` 交给引擎，也没有显式标记 `metadata.afr_replay_context = "task_only"`。

最后一条意味着：`initial_state` 不是"可选的优化"，而是复现可信度的前提。默认只构造 task 消息的快捷路径
必须由录制方显式声明，否则引擎不会假装状态已被恢复。早期版本录制的 Run 没有这个标记，需要重新录制。

两条元数据的方向不要写反：`afr_replay_context = "task_only"` 写在**父 Run**（录制方）上，
是录制方对自己录了什么的自述；引擎读的是父 Run 的这个字段。回放 Run 只有在真的走了
task-only 快捷路径时才带这个标记——调用方显式传了 `initial_state` 时不会被打上，
否则一次真正恢复了状态的回放会反过来声称自己只跑了 task 消息。

这五类边界各自有测试守着，缺证据时引擎给出结论而不是继续跑：

| 边界 | 测试 |
| --- | --- |
| 录制边界缺失 / `seq` 缺口 | `sdk/tests/test_replay_engine.py::test_recording_without_boundaries_is_rejected`、`::test_event_sequence_gap_is_rejected` |
| 脱敏数据 | `::test_redacted_recording_is_rejected` |
| 录制丢失标记 | `::test_recording_loss_marker_is_rejected` |
| 消息条数被截断 | `::test_truncated_model_input_is_rejected` |
| 缺少录制结果 | `::test_recorded_model_response_raises_when_missing`、`::test_unmatched_tool_call_returns_none_instead_of_guessing` |
| 初始状态未被恢复 | `::test_initial_state_is_required_when_the_parent_recorded_one`、`examples/langgraph_sre_agent/tests/test_scenario.py::test_replay_refuses_to_guess_the_state_when_the_parent_recorded_one` |

### 截断边界兜住了什么

表里的「截断上下文」边界，判定依据是**消息条数**：录制时只保留最近 `MAX_MESSAGES`（80）条消息，
若某次模型调用实际收到的消息条数多于保存下来的条数，`message_count` 就会大于 `messages` 的长度，
引擎据此判为 `unsupported_context: truncated messages`。

它**不覆盖**序列化层的字符串截断：`to_jsonable` 会对超长字符串做单值截断（`MAX_STRING` = 20000，
超出部分替换为 `...[truncated N chars]` 标记）。这只改变单条消息内部的文本，不改变消息条数，
`message_count` 与 `messages` 长度依然相等，因此不会触发 `unsupported_context`。读到
`...[truncated N chars]` 标记时应当知道那个字段不是原文，不能把它当作完整上下文。

也就是说，这个边界保证的是「没有整条消息被丢掉」，不保证「每条消息的文本都完整」。

### 回归（Regress）

目标：**验证改动是否让结果变好**，回答"改完到底有没有变好、有没有引入新问题"。

配置：模型真实执行（可以换 Prompt、换模型），工具沿用录制结果。

工具沿用录制结果不是偷懒，而是必需的：回归要比较的是 Agent 的推理与决策，如果工具数据同时漂移，两次运行就不可比了。

## 2. 策略解析

每一步生效的 effect policy 按固定优先级解析：

```
by_seq[seq]  >  by_kind[event_kind]  >  default  >  "recorded"
```

单步覆盖永远赢。这让"只让第 7 步真实调模型"这种调试动作成为一等能力，而不是特例代码。

## 3. 副作用安全

回放一个 SRE Agent 时，它可能再次删除 Pod、再次发出工单。只读 trace 的可观测性工具不需要回答这个问题，因为它们是只读的；本平台会重新执行，所以必须回答。

规则：

1. `write` / `external` 工具在回放中默认强制 `dry_run`。
2. 上层策略写 `live` 也不足以放行，必须同时设置 `allow_side_effect_execution = true`。
3. 真实执行时写入 `attributes.severity = "warning"` 的高可见度事件。
4. 被拦截的步骤产出的合成结果标记 `attributes.synthetic = true`，在时间线上与真实结果视觉区分。

**默认拒绝，显式放行，留痕可查。**

## 4. 确定性边界

必须诚实声明平台做不到什么：

1. **上游漂移**：模型 provider 会下线模型版本、alias 会指向新权重，所谓"精确复现"在 `live` 路径下物理上不可能。要精确复现就用 `recorded`。
2. **时间的不可复现**：断言涉及"当前时间"的工具在回放中会得到不同结果，除非工具自身支持注入时钟。
3. **外部状态漂移**：即便工具结果是录制的，外部世界已改变，因此 `dry_run` 的"本应做什么"不等于"当时做了什么"。
4. **非确定性推理**：`live` 路径下同输入不保证同输出，这正是需要多次采样与断言而非逐字节 diff 的原因。

## 5. 分叉检测

回放结束后，按 `seq` 对齐父 Run 与回放 Run，找出第一个行为不同的步骤：

- `tool_sequence`：工具调用序列出现分歧
- `tool_args`：同一个工具但参数不同
- `model_output`：模型输出文本或工具选择不同
- `error`：一侧报错另一侧没有
- `length`：一侧提前结束或延长

第一个差异点即"Agent 从这里开始走上另一条路"，是 Diff 视图的锚点。

## 6. 回放的执行位置

回放引擎位于 SDK 内（`agent_flight_recorder.replay`），是框架无关的核心 + 框架适配层。本地 MVP 中由服务端作为宿主调用后台任务执行，这是本地部署的选择，不是架构限制：真实用户可以完全在自己的进程里加载历史 Run 并回放，把结果再上报回平台。

宿主执行时按**Agent 自己声明的 runtime** 选适配层（`AgentSpec.runtime`，默认 `langgraph`），
因此平台侧不需要认识任何具体框架：LangGraph 的 Agent 走 `langgraph_adapter`，OpenAI Agents SDK
的 Agent 走 `openai_agents_adapter`，而两个适配层共用同一套成因恢复与失败收尾（`replay/boundary.py`）。
适配边界与「验证到哪一层」见 `docs/architecture.md` 第 8 节。

## 7. 事件来源标注

### 7.1 pi 专用的第三种工具来源：快照重执行

pi 运行器（`integrations/pi/`）在回归时可以选择 `--tool-source snapshot`：只读工具不再消费录制结果，而是在固定提交的工作树上真实执行。

这不是放宽安全要求。允许这么做的前提有两条，缺一不可：

1. 工具集是全只读的（`read` / `grep` / `find` / `ls`），不可能产生副作用；
2. 目标仓库被固定在某个提交上，证据不会随时间漂移——"两次运行可比"的要求由快照而不是由录制结果来满足。

实测依据：同一批任务用严格重放时 30 个样本里 28 个因为模型换了查询参数而无法判断，改用快照重执行后全部得到明确结论，且没有一个答案是错的。参见 [pi 回归验证记录](pi-validation.md)。

事件来源的读法不变：`live` 表示该步真实执行过。选择快照重执行的回归运行里，工具步骤是 `live`，不是 `recorded`——界面必须照实显示。

每个回放事件的 `effect_source` 说明它的结果从哪来：

| 值 | 含义 |
| --- | --- |
| `live` | 真实执行 |
| `recorded` | 取自父 Run 的录制结果 |
| `dry_run` | 副作用被拦截，返回合成结果 |
| `blocked` | 被安全策略拒绝执行 |

时间线必须把 `recorded` / `dry_run` / `blocked` 与 `live` 明确区分显示。一次回放里有多少步是真的跑过的，不能被视觉掩盖。

## 8. 「无法判断」的成因分类

`inconclusive` 不是终点，而是一组**可判定的成因**之一。本平台把「为什么无法判断」
升级为一等判定：有结构化、跨语言共享的分类，服务端与 pi 一致，控制台按成因给出可操作的说明。

### 8.1 码与说明分离

成因的载体恒为两个字段，职责不可互换：

| 字段 | 作用 | 示例 |
| --- | --- | --- |
| `code` | 稳定、可判定的机器取值（下面闭集之一） | `recording_loss` |
| `detail` | 给人看的具体信息（哪个工具、哪一步、缺了什么） | `服务端记录器丢事件（dropped=3）` |

历史上 `reason` 是自由字符串，同时承载「码」与「散文」（例如 `incomplete_recording: missing boundary or event sequence gap`），
调用方无法稳定判定。现在这类字符串只在兼容解析时出现，且会被拆回 `code` + `detail`：
**散文不得再充当码**。

### 8.2 分类是闭集

集合之外的取值一律落到 `unknown`，并**原样保留原文**在 `detail`，绝不被当成某个已知成因。
控制台对未知取值也只说「未知成因」，不会猜到某个具体的下一步。

### 8.3 副作用被拦 ≠ 录制不完整

这是本轮最重要的一条语义修正。两种情况的**成因完全不同**：

| 情况 | 含义 | 用户该看什么 |
| --- | --- | --- |
| 录制不完整 | 拿不到可信结论 | 录制质量（要不要重录） |
| 副作用被拦截 | 这次执行本来就没有真实发生 | 副作用策略（要不要显式放行） |

用例判定不再把两者合并成同一个 inconclusive：前者给录制层面的码，后者给 `side_effect_blocked`。
「副作用被拦住」是安全策略正常生效的结果，不是「结论不通过」，也不该与 failed 共用一个图标。

### 8.4 跨语言对齐清单

三处定义必须是同一个闭集，取值逐字一致：

| 语言 / 层 | 位置 | 形态 |
| --- | --- | --- |
| Python（SDK） | `sdk/agent_flight_recorder/replay/reasons.py` | `InconclusiveCode`（`StrEnum`）+ `InconclusiveReason` |
| TypeScript（pi） | `integrations/pi/src/reasons.ts` | `InconclusiveCode` 联合类型 + `reasonOf` / `legacyReason` |
| TypeScript（控制台） | `web/src/utils/format.ts` | `INCONCLUSIVE_CODES` + `CAUSE_INFO`（中文说明与下一步） |

服务端不重复定义分类，而是复用 SDK 的；`server/tests/test_reason_contract.py` 从上面三处
**读取真实定义**并断言集合逐字相同，因此「每层各自加一个枚举、然后互相不完全一致」会在测试里失败。

| 成因码 | Python（SDK / 服务端）真实产生路径 | pi 真实产生路径 | 含义 | 控制台给出的下一步 |
| --- | --- | --- | --- | --- |
| `incomplete_recording` | SDK `ReplaySession.validate_recording`（缺 run_started / run_finished 边界） | pi `load` 拒绝不受支持的包结构、`runner` 拒绝不完整的父 Run、`cli` 拒绝被改过的用例包 | 缺少录制边界，或包结构不受支持 | 建议重新录制这次运行 |
| `event_sequence_gap` | SDK `ReplaySession.validate_recording`（seq 不连续） | pi `Tape.finish`（还有录制步骤没被消费） | 事件 seq 不连续（丢过事件） | 建议重新录制这次运行 |
| `redacted_replay_data` | SDK `ReplaySession.validate_recording`（父 Run 或事件脱敏过） | pi `save`（落盘前命中脱敏规则） | 证据已脱敏，不再逐字可比 | 用未命中脱敏规则的录制重跑 |
| `recording_loss` | SDK `validate_recording`（`metadata.afr_recording.complete = false`）；服务端 `replay_runner`（记录器丢事件 / 批次失败） | 解析兼容，暂无产生路径 | 录制方/记录器丢过事件 | 建议重新录制这次运行 |
| `truncated_context` | SDK `validate_recording`（`message_count` 大于实存 messages） | pi `runner`（模型输出被长度上限截断） | 模型上下文或输出没有被完整保存 | 提高录制上限后重新录制 |
| `missing_recorded_response` | SDK `recorded_model_response`；两个适配层共用的 `replay/boundary.py`（工具步骤没有匹配的录制结果） | pi `Tape.take`（找不到匹配的录制步骤） | 父 Run 没有这一步的录制结果 | 改用回归模式让工具真实执行 |
| `missing_initial_state` | SDK `ensure_replay_context` | 解析兼容，暂无产生路径 | 父 Run 记了 input，但状态没恢复也没声明 task_only | 提供状态或显式声明只跑 task |
| `model_context_changed` | 解析兼容，暂无产生路径 | pi `runner` 复现路径（模型上下文与录制不一致） | 模型上下文与录制不一致 | 确认模型 / Prompt 是否被改动 |
| `final_output_changed` | 解析兼容，暂无产生路径 | pi `runner` 复现路径（复现结论与录制不同） | 复现结论与录制不同 | 看运行对比定位第一个分叉点 |
| `side_effect_blocked` | 服务端 `cases.py` 用例判定（闸门把写操作降级为 dry_run，或策略直接拒绝） | 解析兼容，暂无产生路径 | 副作用被闸门拦截，本次执行没有真实发生 | 确认安全后可显式允许真实执行 |
| `budget_exceeded` | SDK `ReplaySession.next_step`（步边界上账目已达上限，且这一步真要真实执行） | pi `src/runner.ts`（声明了上限的回归在步边界停下） | 达到声明的成本 / 调用次数上限而停止，这次回放没有跑完 | 提高上限或缩小回放范围后重跑 |
| `unknown` | SDK `from_code` / `legacy` 兼容解析兜底；服务端 `cases.py` 的失败路径 | pi `runner`（非 `Incomplete` 的异常）、`cases.ts`（包不完整且没有成因） | 集合之外的取值 | 按保留的原始说明排查，并在上游补齐分类 |

「**解析兼容，暂无产生路径**」是如实标注，不是待办：这些码在**读取**历史数据时必须认出
（见 8.5 节），但当前没有任何一条代码路径会产出它们。本轮不为表格好看去造产生路径；
真需要时另开 issue。另外注意 pi 的 `load` 会把包里已有的 `cause` 原样上抛（转发，不是生产），
所以 pi 侧可能看到一个它自己不产生、而由服务端写下的码。

`budget_exceeded` 在 pi 侧的产生路径是**声明式预算**：`integrations/pi/src/runner.ts` 只在
这一次执行真的声明了上限（`--budget-models` / `--budget-cost-usd`）时才启用账本，并在步边界
抛出带 `budget_exceeded` 的 `Incomplete`（包 `complete = false`、Run 状态 `aborted`、判定
`inconclusive`）。

运行器自己的安全上限（`--max-models / --max-tools / --timeout-ms`）不属于这条路径：它们是
运行器的运行限制，触顶时仍走执行错误（成因 `unknown`、状态 aborted）。因此**不声明预算的
执行行为与加这套能力之前逐字一致**；两侧都由 `integrations/pi/test/budget.test.ts` 钉住。

这张表不是说明文字：`server/tests/test_reason_contract.py` 会**读它**，把「声称某侧能产生」的
集合拿去真实源码里核对产生点是否存在，并反过来要求标注「解析兼容」的码在那一侧的源码里
确实找不到产生点。每个真实产生路径也都有对应测试（见 `sdk/tests/test_replay_reasons.py`、
`server/tests/test_cases.py`、`server/tests/test_replay_reason_api.py`、
`examples/langgraph_sre_agent/tests/test_inconclusive_reasons.py`）。

### 8.5 历史数据的兼容路径

历史数据里已有的自由文本 `reason` 不得导致崩溃或错误分类。解析规则固定为：

1. 整体就是闭集里的码 -> 直接取码；
2. `码: 说明` -> 拆成 `code` + `detail`；
3. 已知的旧别名 -> 收敛到共享分类的同一个码（例如服务端旧值 `replay_recording_loss` -> `recording_loss`）；
4. 前缀有歧义时以更具体的后半句为准（`unsupported_context: truncated messages` -> `truncated_context`）；
5. 其余 -> `unknown` + 原文。

服务端在读取时**只归一化一次**：`metadata.afr_replay.cause` 与用例的 `last_cause` 恒为
`{code, detail}`，控制台因此不需要自己再写一份解析，也就不会出现第三个码表。

## 9. 回放预算：事先声明、执行硬停、事后如实记账

一次回放可能乘出几十次真实模型调用（一批用例 × 多个条件，见 PRD 的风险表），因此
「这次最多花多少」必须在**提交前**说清楚、**执行中**硬性生效、**结束后**如实记账。

### 9.1 上限声明在 SDK 上

`ReplayPlan.budget` 接受两个维度：最大成本（USD，`max_cost_usd`）与最大模型调用次数
（`max_model_calls`）。两个都不声明就是「不设上限」，此时回放行为与没有这套能力时逐字一致。

上限在 SDK 层生效，而不是只在服务端：回放引擎本来就要求能脱离平台独立运行（第 6 节），
预算不能是平台特有的能力。服务端的回放请求只是把这个字段透传下来。

### 9.2 只约束真实发生的调用

账本只记 `live` 的模型调用：

* 复现模式全程读取录制结果，不产生真实调用，因此**既不消耗次数也不消耗成本**，也不会被
  预算逻辑误判成触顶——复现的确定性保证不受影响；
* `dry_run` 的步骤本来就没有真实发生，同样不进账；
* 分叉点之前的步骤一律按录制结果复现，不计入。

### 9.3 步边界硬停，不做预测性中断

达到任一上限时，引擎在**下一个步边界**停止，不再发起新的模型调用。已经发出的那次调用
允许跑完并如实记账——「这次已经花掉了」是事实，把它从账上抹掉才是失真。

### 9.4 触顶是第三种结论，不是失败

因预算停止的回放**既不是「跑出结论了」，也不是「录制有问题」**，因此它的成因码是新增的
`budget_exceeded`，落在 `inconclusive` 一侧：

| 层 | 表现 |
| --- | --- |
| 引擎 | 抛 `ReplayExhaustedError`，`cause.code = budget_exceeded`，`detail` 带上实际用量与上限 |
| 回放 Run | 状态 `aborted`（不是 `failed`），`metadata.afr_replay.complete = false` 且带成因 |
| 用例 / 批量 | 判为 `inconclusive`：批量里它算「拿不到结论」，不算「未通过」 |
| 控制台 | 「因预算停止」+ 已用 / 上限 / 是否触顶，而不是普通失败 |

把它写成 `failed` / `error` 就是把「没跑完」说成「不通过」，这正是第 2、5 轮各返工过一次的
同类错误。`budget_exceeded` 由引擎真的产生（`ReplaySession.next_step`），不是只声明；pi 侧
同样由真实路径产生（`integrations/pi/src/runner.ts`，声明了上限的回归在步边界停下）。

### 9.5 事后记账：已用 / 上限 / 是否触顶

声明了上限的回放，其 Run 元数据里带 `metadata.afr_replay.budget`：

| 字段 | 含义 |
| --- | --- |
| `max_cost_usd` / `max_model_calls` | 声明的上限；`null` 表示这一维不参与判定 |
| `model_calls_used` | 已用：真实发生的模型调用次数 |
| `cost_used_usd` | 已用成本（估算）；`null` = **未知**，不是 0 |
| `cost_unknown` | 是否存在无法定价的真实调用（成本记成「未知」的原因） |
| `exceeded` / `stopped_by` | 是否触顶、触的是哪一维（`model_calls` / `cost`） |
| `detail` | 一句人话说明 |

成本未知时是「未知」而不是 0：模型不在价格表内、或这次调用没有 token 记录时，账本就是
`null`。此时**成本上限这一维无法判定**——不会因为「算不出来」就假装触顶，也不会把未知当成
0 说「没花钱」。调用次数上限不受影响，仍然按次数硬停。

### 9.6 预估：提交前先知道大概

`estimate_replay_budget(plan, parent_run, parent_events)` 只读父 Run 的录制与计划，
**不调用任何模型**，也不创建 Run。它给出预计的模型调用次数与估算成本，并明确标注为估算：
价格表是本地快照，回归模式下新上下文与父 Run 也不完全相同。

父 Run 缺少可用 token 数据、或模型不在价格表内时，预估如实返回「无法预估」（`cost_usd = null`），
而不是给一个编造的数字。计划里没有任何 live 模型调用时，预估是 0 次调用、0 成本——那是事实，
不是猜测。

### 9.7 pi 侧：同一套语义，只在声明预算时启用

pi 运行器的批量预算（`--budget-models` / `--budget-cost-usd`）与本节是**同一套语义**，不是
第二套字段名，也不是第二套判定：上限沿用 `max_cost_usd` / `max_model_calls` 两个维度，到达
上限即停（步边界判定，已发出的调用照常记账），触顶取 `stopped_by` 的同一组机器值，成本未知
**不是 0**（未知时成本这一维不参与判定）。

停止时抛带 `budget_exceeded` 的 `Incomplete`：回放包 `complete = false`、状态 `aborted`、
判定 `inconclusive`。整批的调度与 Python 侧同源：账到点之后**先记账再**把剩下的格子标成
`not_started`（没有结论，也没有成因）。

### 9.8 这一层不做的

* 不做真实模型的费用对账：价格表仍是本地快照，所有数字都标注为估算。
* 不做历史成本曲线与通过率趋势。
* **批次级的聚合预算**不在这一层：一次回放只知道自己的上限。整批的上限与「未启动的格子
  如何处置」是另一层的能力，见第 10 节。

## 10. 批次级聚合预算：整批的上限与「未启动」的格子

一次批量套件会派生出 N×M 次回放（用例 × 条件）。第 9 节的预算只管得住**其中一次**；
「这一批总共最多花多少」必须有地方声明、有地方拦住。这一节是那件事。

### 10.1 上限声明在提交套件时

`POST /v1/suites` 的请求体接受 `budget`，两个维度与单次回放**完全一样**
（`max_cost_usd` / `max_model_calls`，同一份 `ReplayBudget`）。`null` 或不传 =
不设上限，此时套件的调度、状态取值与汇总字段与没有这套能力时逐字一致。

上限只约束**真实发生的模型调用**：复现模式的格子读的是录制结果，本来就不花模型钱，
因此既不消耗整批的次数也不消耗整批的成本。

### 10.2 提交前可预估整批

`POST /v1/suites/estimate` 是只读的：它把每个格子的预估聚合起来，返回整批的预计调用
次数与估算成本。它**不调用任何模型，也不创建批次**，用的是与真的执行同一套计划解析与
同一份录制。

缺数据时整批就是**无法预估**：只要有一格给不出成本，整批的 `cost_usd` 就是 `null`，
并给出原因。**不把已知的部分加起来冒充整批成本**——那等于把未知当成 0。
计划里没有任何一步真实调用模型时，预估是 0 次调用、0 成本：那是事实，不是猜测。

声明了上限时，预估还要说清**这个上限到底约束了什么**，不许把「守住了」当默认结论：

* 调用次数上限是硬的：次数在发出之前就知道，因此预估会说「最多跑这么多次」；
* 成本上限只有在**成本算得出来**的时候才可能到点。成本未知（模型不在价格表内）时
  `exceeded_by()` 会跳过成本判定，这一维**根本不参与判定**，预估必须如实说这一维无法判定、
  并指出实际的约束落在哪一维（两个维度都给不出来时，就是「这一次声明没有产生实际约束」）；
* 成本算得出来且预估超过上限时，**不把预估数字改写成上限值**（那会读成「预计就花这么多」），
  而是说明这一维的真实行为：到点后在步边界停止、不再发起新的调用，已经发出的那次调用允许
  完成并如实记账，因此实际成本可能比上限多出最后一次调用。

### 10.3 硬停：不再启动新格子，已启动的格子跑完

声明了整批上限的批次**按顺序逐格执行**，每一格拿到的额度是「整批剩余」：

1. 启动一格之前先看整批的账：已经到点就不再启动任何新格子；
2. 已经启动的格子**不会被预测性中断**：它带着自己的剩余额度跑，在步边界停下（与第 9 节
   同一条规则），已经发出的那次调用允许完成并如实记账；
3. 上一格真实用掉的部分在**下一格启动之前**已经进了整批账本，因此
   「整批实际用量 ≤ 上限」是一条**可保证的结论**，而不是一场竞态。

为什么必须逐格：两格并发启动时，它们各自看到的「剩余」都会是启动前的那一份，两边加起来
就已经越过整批上限了。要同时满足「整批不超过上限」与「不做预测性中断」，只能让
「已用多少」在启动下一格之前是确定的。

两个维度的保证强度不一样，这一点必须说清楚：**调用次数**在发出之前就知道，因此它是硬上限
（整批真实调用次数一定不超过它）；**成本**与单次回放的第 9.3 节同一条——账到点之后不再发起
新的调用，但**已经发出的那一次调用的成本仍然会发生**，所以总成本可能比上限多出最后一次调用。
不做预测性中断的代价就是这个，平台不把它说成「一定不会超」。

还有一层更彻底的例外：**成本未知时，成本上限这一整维不会生效**。`BudgetLedger.exceeded_by`
在成本为 `None`（模型不在本地价格表内）时跳过成本判定——这是「未知不是 0」的必然结果，
不是漏实现。此时只有调用次数上限在约束这一批；两个维度都不生效时，这一批实际上没有上限，
界面与预估都必须这么讲，而不是说「守住了一个上限」。

### 10.4 未启动的格子是第三种未完成态

到点之后没轮到的格子状态是 `not_started`，呈现为**「未启动（因批次预算用尽）」**。
它不是 failed、不是 inconclusive、也不是 error：

| 状态 | 含义 | 有没有结论 |
| --- | --- | --- |
| `pending` | 还没轮到 | 没有 |
| `running` | 正在执行 | 没有 |
| `not_started` | **不会再轮到**（整批预算已用尽） | 没有 |
| `passed` / `failed` | 跑完了，结论是明确的两态之一 | 有 |
| `inconclusive` / `error` | 跑过了，但拿不到可信结论 | 有（成因） |

因此它归在 `unfinished` 一侧（三个桶依然互斥且穷尽：
`total == determinable + undecided + unfinished`，其中
`unfinished = pending + running + not_started`），**绝不计入 failed，也不计入 undecided**
——后者说的是「跑过了但拿不到结论」，对一个从未执行过的格子说这句话是凭空断言。

未启动的格子**没有成因**（`cause` 恒为 `null`）：成因只属于跑过了的两类。原因由状态
本身加上整批的记账一起表达，不替一个没跑过的格子编一条成因。

措辞分两级，边界要写清：**批次级**的记账句子绝不出现「跑完了 / 全部通过 / 拿不到结论」；
**分组级**的句子只讲自己那 N 条——含未启动格子的分组说「已完成 X、未完成 Y（其中 Z 条
未启动）」，而一个自己已经跑完、且真有格子被判为 inconclusive 的分组照实说「N 条中 M 条
拿不到结论」，这里的 M 是**那几条真的跑过、真的没拿到结论**的格子。把后者也一并压掉，
等于为了让整批的话看起来干净而隐瞒一个已经成立的判定。

### 10.5 事后记账

`GET /v1/suites/{id}` 的 `budget` 给出整批的对照，字段与单次回放的记账**完全一样**
（同一个 `BudgetUsage`）：

| 字段 | 含义 |
| --- | --- |
| `max_cost_usd` / `max_model_calls` | 声明的整批上限；`null` 表示这一维不参与判定 |
| `model_calls_used` | 整批已用：所有格子真实发生的模型调用次数之和 |
| `cost_used_usd` | 整批已用成本（估算）；`null` = **未知**，不是 0 |
| `cost_unknown` | 是否存在无法定价的真实调用 |
| `exceeded` / `stopped_by` | 整批是否因触顶而停止、停在哪一维（`model_calls` / `cost`） |
| `not_started` | 因批次预算用尽而**没跑**的格子数 |
| `detail` | 一句人话说明（触顶时不含「跑完了」「全部通过」「拿不到结论」这类措辞） |

没声明过整批上限的批次**不带**这个字段——「没声明」不等于「上限为 0」，也不该凭空多出
一个「上限：无」的账目，这与第 9.5 节对单次回放的要求是同一条。

界面上的两个词要分清：**生命周期**与**结论**不是一回事。`status = finished` 只说明不会
再有格子启动了；触顶而停止的批次`finished` 但没跑完，因此它的角标只能说**「已停止」**，
不能说「已完成」——后者在同一张卡片上已经被 `completed` 这个计数占用了（
「已完成 X / Y」），同一个短语指向两个数正是本项目的表述缺陷；对一次被预算拦下的批次
说「已完成」还等于把「没跑完」说成「跑完了」。

### 10.6 与单次回放的额度是什么关系

两者不是两套账，也不会互相覆盖：

* 格子拿到的额度就是**整批剩余**，因此它天然不会越过整批上限；两层用的是同一条判定
  （步边界、两个维度谁先到谁停）；
* 格子的 Run 记的是**这一次回放自己**的用量，批次行记的是**整批的合计**，两个数字来自
  同一个账本实现（SDK 的 `BudgetLedger`），不是把事件再数一遍的第二套口径；
* 不声明整批上限的套件根本不传账本下来，单次回放路径（`POST /v1/runs/{id}/replay`、
  `POST /v1/cases/{id}/run`）的记账与第 9 节完全一致。

### 10.7 格子状态闭集的三处对齐

新增的 `not_started` 是稳定机器值，三处定义的**取值逐字一致**（顺序按各层自己的可读顺序）：

| 语言 / 层 | 位置 | 形态 | 覆盖范围 |
| --- | --- | --- | --- |
| Python（服务端） | `server/afr_server/suites.py` | `VERDICTS` + `OPEN_STATUSES` | 全部七个状态 |
| TypeScript（控制台） | `web/src/api/types.ts` | `SuiteItemStatus` 联合类型 | 全部七个状态 |
| TypeScript（pi） | `integrations/pi/src/core.ts` | `Verdict` 联合类型 | 只覆盖四个结论态 |

pi 只管**单条**用例的结论，它没有「格子」这个概念（批量套件是服务端与控制台之间的事，
见 [上报协议](protocol.md) 第 7 节），因此它的闭集是七个状态里的**结论子集**：不新增
产生不出来取值的成员，也不为表格好看造一个用不到的状态。这条不对等是如实标注，不是遗漏。

`server/tests/test_suite_budget.py` 从这三处的**真实定义**里读出集合再比较，因此
「每层各自加一个枚举、然后互相不完全一致」会在测试里失败。

---

## 11. 用例集版本：一批结论的归属

回放语义回答「这一次执行按什么规则跑」，版本回答**另一件事**：这一批结论是在**哪一版用例定义**
下得出的。它不改变任何回放语义（复现 / 回归、策略优先级、副作用闸门、预算硬停一个字都没动），
只给批量套件补上「归属」。

**提交那一刻冻结。** 提交套件时读出这一批用到的用例行，各取一份定义快照；每个格子执行的都是自己
那一份快照，执行期不再读用例行。因此：

* 批次执行期间用例被编辑，不会让前半批按旧断言、后半批按新断言——整批共享同一个版本；
* 用例行被删掉也不影响：格子照样按冻结的定义给出结论（读不到行就不该有结论，等于把一次已经定下来
  的判据交给一个可变的外部状态）。

**标识是内容决定的**（`cs1:<sha256>`）。计入 source_run_id、from_seq、to_seq、assertions 与
effect_policy；不计入名字、描述、labels 与 `last_*`。规范化规则：字典键序无关，断言按
`AssertionSpec` 归一化（省略字段与显式 null 是同一条断言），值里的空白有意义（不做 trim），
断言列表顺序有意义（它同时决定断言结果列表的顺序），用例的提交顺序无关。**用例变了**与
**同一版的新结论**因此是两件可分辨的事：前者换标识，后者不换。

**归属与提示分开说。** 历史批次没有版本记录时读出来就是「无版本记录」，**不按当前用例反推一个
版本号**；用例后来被改动时，批次详情给出 drift（哪些变了、哪些找不到了），它只影响提示，不改写
任何一条已经跑出来的结论。跨版本的对比视图、版本迁移 / 合并 / 分支、可编辑的版本名**不在这一轮**。

完整的数据契约见 [上报协议](protocol.md) 第 7 节，数据模型与取舍见 [架构设计](architecture.md)
第 4.5、7 节，规范化的逐条规则钉在 `server/tests/test_suite_versions.py`。

