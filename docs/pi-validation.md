# pi 回归验证记录

| 项 | 值 |
| --- | --- |
| 状态 | 已用真实模型完成两轮对照；结论见下 |
| 验证对象提交 | `da1038c15ad5f010416924270ff4a0e17a15169f` |
| 模型 | `opencode-go/deepseek-v4.1-flash` |
| 运行器 | `integrations/pi/`，pi coding-agent SDK 0.85.1 |
| 最近更新 | 2026-09-13 |

原始包、`results.json` 与 `report.md` 位于 `integrations/pi/artifacts/`（已 gitignore）。它们来自 `opencode-go` 网关，不是离线剧本模型。

---

## 1. 两轮对照：录制结果 vs 仓库快照

同一套 5 个任务、同样的 baseline / grounded 两个 Prompt、每组 3 次采样，共 30 个回归样本。唯一变量是回归时工具结果的来源。

| 回归工具来源 | 可判断 | 通过 | 失败 | 无法判断 | 平均模型调用 | token |
| --- | --- | --- | --- | --- | --- | --- |
| `recorded`（严格重放录制结果） | 2/30 | 1 | 1 | 28（93%） | 1.6 | 244,803 |
| `snapshot`（固定快照上重执行只读工具） | 30/30 | 25 | 5 | 0 | 4.2 | 804,389 |

**这是本轮最重要的发现。** 严格重放要求工具名、参数、次序逐项一致，而模型即使在同一 Prompt 下也会更换 grep 模式或换一个目录去查。结果不是"回归失败"，而是根本拿不到结论：30 个样本里 28 个在第一步或第二步就停住。

固定提交加只读工具集之后，证据不可能漂移，因此可以放开工具真实执行；`snapshot` 模式据此把可判断率从 7% 提到 100%，代价是每个样本的模型调用从 1.6 次升到 4.2 次（它真的在调查）。

## 2. 失败分类：没有一个是答错的

`npm run audit` 把"没按格式输出"和"答错了"分开。30 个 `snapshot` 样本：

| 分类 | 数量 | 含义 |
| --- | --- | --- |
| `pass` | 21 | 结构化输出，答案与引用都正确 |
| `format_only` | 7 | **答案与引用都正确**，但在 JSON 前后附加了解释文字，严格解析失败 |
| `bad_evidence` | 2 | 答案正确，引用的行号或原文对不上 |
| `wrong_answer` | 0 | — |

也就是说，只看内容正确性时是 28/30。用例判定里那 5 个 `failed`，大部分其实是格式问题。**如果不看这个分类，会把模型的格式习惯误读成能力缺陷。**

## 3. 修正版 Prompt 没有带来改善

| 判定口径 | baseline | grounded |
| --- | --- | --- |
| 严格（结构 + 内容） | 14/15 | 11/15 |
| 仅内容正确性（audit） | 15/15 | 13/15 |

两种口径下 grounded 都**没有**更好，反而少通过 2~3 个样本。两个 `bad_evidence` 也都出现在 grounded 组。

原因不是 Prompt 变差，而是**任务太简单**：baseline 已经全部答对，没有可修复的失败，自然看不出修正版 Prompt 的价值。这与计划里"两组都通过 → 重新设计任务"的分支一致。

因此本轮**没有观察到任何"失败 → 通过"的真实修复**，不声称修复效果。

## 4. 固定提交上的事实核查

五个任务的预期答案与引用行在运行前由脚本逐条比对源码验证通过（`validate.ts` 会拒绝过期的预期引用）。正确答案记录在 `validation/suite.json`。

人工构造的负向控制（把答案故意改成相反值）在两轮中都稳定判为失败，说明断言本身能区分对错。

## 5. 成本

两轮合计约 105 万 token（含 5 次录制）。单个 `snapshot` 回归样本平均 22,766 token、4.2 次模型调用、4.4 次工具调用，p95 耗时约 20 秒。用 `deepseek-v4.1-flash` 这个量级可以频繁重跑，这也是先用它做验证的原因。

## 6. 复现方式

```powershell
cd integrations/pi
$env:MSEE_PI_GATEWAY_KEY = "<opencode-go key>"
npm run validate -- --repo ../.. --provider opencode-go --model deepseek-v4.1-flash `
  --models-path artifacts/models.json --tool-source snapshot --samples 3 --out artifacts/validation
npm run audit -- --dir artifacts/validation
```

## 7. 仍未验证 / 已知限制

 - ~~**任务区分度不足**~~：已在第 8 轮补上——新任务集 `validation/failure-suite.json`（5 个 baseline 真的会失败的任务）与两轮真实模型结果见 [真实失败任务集](pi-failure-suite.md)。那一轮同时确认了本页的旧结论：可区分性来自**证据链要求**，而不是任务本身的推理难度。
- 每个条件只有 3 次采样，且只跑了一个模型；结论不外推到其他模型。
- 断言要求精确行号与原文，对引用质量是严格口径；`format_only` 说明这个口径与模型的输出习惯有摩擦。
- 只读调查以外的任务（编辑文件、执行命令）没有接入，也没有回放语义。
- 除 pi 与 LangGraph 离线示例外，还没有第三条真实 Agent 路径。
