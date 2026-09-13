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

