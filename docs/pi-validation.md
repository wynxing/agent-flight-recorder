# pi 回归验证记录

| 项 | 值 |
| --- | --- |
| 状态 | 离线契约与运行器已交付并验证；真实模型样本**未执行**（凭据受限） |
| 验证对象提交 | `da1038c15ad5f010416924270ff4a0e17a15169f` |
| 运行器 | `integrations/pi/`，pi coding-agent SDK 0.85.1 |
| 最近更新 | 2026-09-13 |

本文档只记录实际观察到的事实。没有执行的部分写"未执行"，不用离线演示代替真实模型结论。

---

## 1. 已完成并验证的部分

**回放可信度修复**（Python 测试通过）

- 父运行轨迹结束之后新增的真实工具步骤，现在同样经过副作用闸门。此前该路径只解析执行模式、直接返回 `live`。
- 缺少匹配的录制工具结果时回放立即停止，不再返回标记为成功消息的合成结果继续推理。
- 用例结论区分 `passed / failed / inconclusive / error`；录制不完整不可能判通过。
- 回放前校验事件边界、序列缺口、脱敏与截断上下文；不满足则记为 `inconclusive`。

**pi 运行器离线契约**（`npm test`，7 项通过）

- 用真实 pi coding-agent SDK 录制一次 `read` 调用，再在**不访问原工作树、不调用 provider** 的前提下完成复现，行为步骤与原文一致。
- 回归模式下新 Prompt 改变工具参数时，运行器在对应步骤停止并返回 `inconclusive`，不回退真实工具执行。
- 同名同参数结果只被消费一次；提前结束复现会报 `early_end`。
- 路径穿越、符号链接与 Windows junction 被拒绝；被拒绝的动作不进入录制输入。
- 含凭据内容在落盘前脱敏，脱敏后的包标记为不完整并拒绝可信回放。
- 模型与工具预算真实中断 SDK 循环；provider 错误保留为 `error` 而不是"无法判断"。
- `models.json` 的 `!command` 形式被拒绝，配置不能变成任意命令执行。

**跨语言链路**（`server/tests/test_pi_protocol.py`）

- TypeScript 运行器产出的载荷通过 Python `IngestRequest` 校验、`POST /v1/ingest` 入库并在控制台可见；pi 运行在服务端发起回放时返回 409，指向本地运行器。

**验证脚本按预期失败**

对固定提交执行 `npm run validate` 时，脚本先逐条核对 5 个任务的预期源码引用是否仍然匹配（全部匹配），随后在第一次真实模型请求处停止，并写出 `results.json` 与 `report.md`。临时工作树在结束后被移除，`git worktree list` 只余主仓库与本工作树。

---

## 2. 真实模型样本未执行的原因

按计划需要 provider 与凭据。当前环境里可用的三个网关都无法完成一次真实请求：

| 网关 | 模型 | 观察到结果 |
| --- | --- | --- |
| opencode（付费模型） | `claude-sonnet-4-6`、`glm-5.3-flash`、`minimax-m3` | `401 CreditsError: Insufficient balance` |
| opencode（免费模型） | `deepseek-v4-flash-free`、`nemotron-3-ultra-free`、`mimo-v2.5-free` | `400 MissingSessionID: OpenCode's free tier can only be used in OpenCode`；`deepseek-v4-flash-free` 另报 `Model is unavailable` |
| Agnes | `agnes-2.5-pro`、`agnes-3.0-flash` | `503 model_not_found / No available channel`；`agnes-3.0-flash` 60 秒超时 |

本机无 `~/.pi/agent/auth.json`，`ollama`、LM Studio 等本地推理服务未运行（11434 / 1234 / 8080 / 8000 均无响应）。

因此 `integrations/pi/validation/suite.json` 中的 5 个任务虽然已写好、预期引用已人工核对，但**没有任何模型样本被执行**。通过率、模型调用量与耗时都没有真实数据，本文档不给出估计值。

复现该阻塞：

```powershell
cd integrations/pi
npm run validate -- --repo ../.. --provider opencode --model claude-sonnet-4-6 --models-path artifacts/models.json --out artifacts/validation
```

`artifacts/` 已被 gitignore。模型配置模板见 `integrations/pi/validation/opencode.example.json`。

---

## 3. 解除阻塞后要做的事

1. 提供可用凭据（或改用网关免费额度覆盖的模型），重跑 `npm run validate`。
2. 核对该次运行是否真的出现"baseline 失败 → grounded 通过"；没有出现就照实报告。
3. 分开统计三类结果：断言失败、录制不足导致的 `inconclusive`、执行错误。只有第一类能作为模型结论证据。
4. 人工复核通过样本引用的源码行，确认不是格式命中而实质错误。
5. 若多数样本因工具参数变化而 `inconclusive`，按计划转向"固定仓库快照上的只读工具重执行"，而不是继续扩充控制台。

---

## 4. 仍未验证的能力

- 单一模型单次采样不代表稳定性；计划里的每个对照组 3 次采样尚未产生数据。
- 只读调查以外的任务（编辑文件、执行命令）没有接入，也没有回放语义。
- pi 之外的第二条真实 Agent 路径（除 LangGraph 离线示例外）仍未接入。
- 复现验证的是 pi 循环与完整消息内容，不是 token 流逐帧复现。
