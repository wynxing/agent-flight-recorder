# 真实失败任务集：让真实模型回答「改完到底有没有变好」

| 项 | 值 |
| --- | --- |
| 状态 | 两轮真实模型运行已完成；结论、失败性质与限制见下 |
| 被测提交 | `b5332540bd702f789088aa90603cc7dd5d1084ed`（`feat/16-real-failure-tasks` 上、加入 pi 批量预算之后） |
| 任务集 | `integrations/pi/validation/failure-suite.json`（5 个任务；`--phase check` 在执行前逐条核对预期引用） |
| 两个条件 | `validation/baseline.txt`（baseline）与 `validation/grounded.txt`（grounded），与第 1 轮 pi 验证用的是同两份 Prompt |
| 模型 | `msee-gateway/deepseek-v4.1-flash`；网关地址与密钥来自环境变量 `API_URL` / `API_KEY`（本仓库不含任何凭证值） |
| 回归工具来源 | `snapshot`：固定提交上重执行只读工具 |
| 采样 | 每个条件每个任务 3 次 × 2 轮 = 6 次样本 |
| 判据 | 任务集自带断言：`no_error` + `json_claim`（答案与**全部**预期引用逐字一致） |
| 日期 | 2026-09-14 |
| 原始归档 | `integrations/pi/validation/evidence/2026-09-14-real-model/`（每样本的结论、成因、调用数、token、成本、模型最终答案） |

这份文档只做两件事：把**观察到的事实**列清楚，再单独写出**据此的推断**。任何一处推断都会标明它是推断。

## 1. 观察到的事实：两轮结果

「通过 / 未通过」是平台用例的严格判据（答案 + 全部引用逐字符合）。括号里是同一条件的样本数。

| 任务 | run 1 baseline | run 1 grounded | run 2 baseline | run 2 grounded | 两轮合计 baseline | 两轮合计 grounded |
| --- | --- | --- | --- | --- | --- | --- |
| `batch-budget-total-calls`（答 2） | 0/3 | 0/3 | 0/3 | 1/3 | 0/6 | 1/6 |
| `cause-precedence-blocked-vs-budget`（答 `side_effect_blocked`） | 1/3 | 3/3 | 3/3 | 3/3 | 4/6 | 6/6 |
| `write-tool-gate-on-recorded-regress`（答 false） | 0/3 | 3/3 | 0/3 | 2/3 | 0/6 | 5/6 |
| `unknown-cost-does-not-stop`（答 false） | 0/3 | 1/3 | 1/3 | 2/3 | 1/6 | 3/6 |
| `pi-tape-mismatch-code`（答 `missing_recorded_response`） | 3/3 | 3/3 | 1/3 | 3/3 | 4/6 | 6/6 |
| **合计** | **4/15** | **10/15** | **5/15** | **11/15** | **9/30** | **21/30** |

两轮的样本量、调用数与成本（成本按本地价目表折算，见第 5 节）：

| | baseline 样本 | grounded 样本 | 录制 |
| --- | --- | --- | --- |
| run 1 | 15 个样本 / 70 次调用 / 653,217 token | 15 / 103 / 1,465,964 | 19 次调用 / 177,874 token |
| run 2 | 15 / 58 / 517,273 | 15 / 97 / 1,340,003 | 23 / 205,550 |

## 2. 观察到的事实：失败是什么性质的

「未通过」不等于「答错」。把每个样本的**内容**分开看（`npm run summarize` 的口径，与 `npm run audit` 同源）：

| 内容分类 | 含义 | baseline（两轮 30 样本） | grounded（两轮 30 样本） |
| --- | --- | --- | --- |
| `pass` | 答案与全部引用都对 | 9 | 18 |
| `bad_evidence` | **答案对**，引用缺失或对不上 | 15 | 4 |
| `wrong_answer` | 答案与预期不等 | 4 | 5 |
| `format_only` | 答案与引用都对，JSON 外面多写了说明 | 1 | 3 |
| `no_json` | 没有可解析的 JSON 答案 | 1 | 0 |

两条必须写明的细节：

1. **全部 9 个 `wrong_answer` 都是同一个原因：把整数答案写成了字符串。** 模型答的是 `"2"` 而不是 `2`，数字结论在 9 个样本里**全对**。也就是说 `batch-budget-total-calls` 在 baseline 下的 6 次「未通过」，没有一次是「答不出 2」：4 次是答案类型（字符串 vs 数字），1 次是 JSON 外面多了说明（`format_only`，内容全对），1 次是引用对不上。
2. **严格判据下最大的失败来源是引用链**：baseline 有 15 次 `bad_evidence`，grounded 有 4 次。这条差异与两个 Prompt 的差别直接对应（grounded 明确要求「引用直接支持结论的代码原文及准确行号」）。

## 3. 观察到的事实：翻转与未翻转（逐任务）

| 任务 | baseline 未通过/总数 | grounded 通过/总数 | 结论 |
| --- | --- | --- | --- |
| `write-tool-gate-on-recorded-regress` | run 1: 3/3；run 2: 3/3 | run 1: 3/3；run 2: 2/3 | **翻转**（两轮都成立） |
| `cause-precedence-blocked-vs-budget` | run 1: 2/3；run 2: 0/3 | run 1: 3/3；run 2: 3/3 | run 1 翻转；run 2 未翻转（baseline 已全对） |
| `pi-tape-mismatch-code` | run 1: 0/3；run 2: 2/3 | run 1: 3/3；run 2: 3/3 | run 2 翻转；run 1 未翻转（baseline 已全对） |
| `unknown-cost-does-not-stop` | run 1: 3/3；run 2: 2/3 | run 1: 2/3；run 2: 1/3 | **未翻转**（两个条件在两轮里都没通过） |
| `batch-budget-total-calls` | run 1: 3/3；run 2: 3/3 | run 1: 3/3；run 2: 2/3 | **未翻转**（两个条件都基本不通过，且失败主因是答案类型） |

按「3 次采样里至少 2 次未通过」这条线，**两轮都稳定失败的只有 3 个任务**：`batch-budget-total-calls`、`write-tool-gate-on-recorded-regress`、`unknown-cost-does-not-stop`；另外两个任务只在其中一轮达到这条线。这是事实，不做修饰。

## 4. 观察到的事实：可判断率

沿用 [pi 回归验证记录](pi-validation.md) 的口径（未执行样本不进分母）：

| 轮次 | 可判断样本 | 无法判断 | 执行错误 | 可判断率 |
| --- | --- | --- | --- | --- |
| run 1 | 30 | 0 | 0 | 30/30（100%） |
| run 2 | 30 | 0 | 0 | 30/30（100%） |

两轮合计 60/60 可判断，没有出现任何 `inconclusive`。这与第 1 轮「`snapshot` 把可判断率从 7% 提到 100%」的结论一致：固定提交 + 只读工具让证据不会漂移。

## 5. 观察到的事实：成本与预算

**先声明上限再开跑**。上限与平台是同一套语义（`max_model_calls` / `max_cost_usd`，步边界硬停，`stopped_by ∈ {model_calls, cost}`）：

| 环节 | 值 |
| --- | --- |
| 声明的上限（run 1、run 2、上限演示三次都一样） | 400 次真实模型调用 / $2.00 |
| 免费预估（录制之后、回归之前，`estimate.json`） | run 1：预计 114 次调用 / 约 $0.100922；run 2：预计 138 次 / 约 $0.103382 —— 都在限额内，才开跑回归 |
| 实际发生（run 1） | 192 次调用 / 2,297,055 token；按本地价目折算约 $0.148380 |
| 实际发生（run 2） | 178 次调用 / 2,062,826 token；约 $0.148015 |
| 实际发生（上限演示，第 6 节） | 4 次调用 / 22,023 token；约 $0.002807 |
| 连通性探测（开跑前直连一次最小请求） | 1 次调用 / 45 token |
| **本轮合计** | **375 次真实模型调用 / 4,381,949 token；按本地价目折算约 $0.299220** |

关于钱的两条必须说清楚：

1. **$0.299 是估算，不是账单。** 它由 `models.json` 里声明的本地价目（0.22 / 0.66 USD per 1M token）乘以实测 token 得出。同网关同族模型 `deepseek-v4-flash` 的价目就是这两个数（来源：pi SDK 内置注册表 `@earendil-works/pi-ai/dist/providers/data/opencode-go.json`）；`deepseek-v4.1-flash` 的官方价目没有查到，因此它是**代理价目**。
2. **本次没有可对账的账单金额。** 直连探测那一次响应里，网关自报 `cost` 字段为 `0`；pi 侧记录的成本一律来自上面那份本地价目。本报告不把任何估算写成实测账单。

预估与实测的差距也是事实：run 1 预估 114 次 / $0.100922，实测回归 173 次 / $0.131558；run 2 预估 138 次 / $0.103382，实测回归 155 次 / $0.130784。**预估偏低**，原因是它按父录制里真实发生的调用量与成本折算，**不包含**回归时超出父录制尾部的新增步骤——这正是第 7 轮审核那条 P2 的量化确认。

## 6. 观察到的事实：预算真的会硬停（真实调用，不是离线测试）

用同一套任务集的一个子集（2 个任务 × 2 个条件 × 1 次采样，录制复用 run 2 的），声明上限 **4 次调用**：

| 格子 | 结果 |
| --- | --- |
| 第 1 格（`write-tool-gate` baseline 1） | 用掉 3 次调用后正常给出结论（未通过断言） |
| 第 2 格（`write-tool-gate` grounded 1） | 拿到剩余 1 次额度，第 4 次调用之后**在步边界停下**：`status = aborted`、`complete = false`、判定 `inconclusive`、成因码 `budget_exceeded` |
| 第 3、4 格（`unknown-cost` 两个条件） | `not_started`：**没有结论，也没有成因**，不计入未通过或无法判断 |

整批账目：`stopped_by = model_calls`、`exceeded = true`、已用 4 次 —— 恰好等于声明的上限，没有超出。归档见 `evidence/2026-09-14-real-model/cap-demo/`。

## 7. 自查与反证

都是可重跑的检查，不是自述：

* **预期引用先校验后付费**：`--phase check` 在花钱之前逐条核对任务集里的引用在固定提交上是否仍然成立。本轮它真的拦下过一次：改了 `runner.ts`（新增批量预算那段）之后，`pi-tape-mismatch-code` 的引用行号过期，check 当场停止（`Stale expected evidence`），改正后才重跑。
* **负向控制**：把答案改成相反值再判定，必须失败。run 2 的归档里有它（`run-2/negative-control.json`，`verdict = failed`）。判据不是恒真。
* **变异反证**：把 `CAUSE_PRECEDENCE` 里 `side_effect_blocked` 与 `budget_exceeded` 的取值对调，`server/tests/test_reason_contract.py::test_side_effect_blocked_wins_over_budget_exceeded_when_both_hold` 立刻变红；还原后全绿。那个任务问的正是这件事，因此它的预期答案是被代码钉住的，不是被文档抄来的。
* **构建会话自己的错，如实记录**：run 1 跑完，归档里 `batch.usage.model_calls_used = 0`，而同一批的样本合计 173 次调用。查因：批量账本**没有把每一格的用量记回来**，于是声明的 400 次上限一直是「已用 0」，一次都没生效——一个看起来声明了上限、实际不存在的上限。修法是 `mergeCell`（整批的已用 = 各格之和，未知成本照旧是未知），并补了会真变红的回归测试。run 2 的账本因此正确（已用 178 = 样本 + 录制的实际用量）。
* **现场对账（抽两个数字回原始归档）**：见第 8 节。

## 8. 怎么复算报告里的数字

归档（已进仓库）：`integrations/pi/validation/evidence/2026-09-14-real-model/`

* `run-1/`、`run-2/`：`archive.json`（每样本的条件、结论、成因、调用数、token、成本、包 id）、`summary.json`（同一批数字 + 每个样本的模型最终答案与内容分类）、`estimate.json`（那次免费预估）、`report.md`（运行器自己写的报告）。
* `cap-demo/`：第 6 节那次硬停的归档。

两个抽样对账（直接对 `run-2/archive.json` 跑）：

```console
$ node -e "const a=require('./integrations/pi/validation/evidence/2026-09-14-real-model/run-2/archive.json');
  const sel=a.samples.filter(s=>s.task==='write-tool-gate-on-recorded-regress'&&s.arm==='baseline');
  console.log(sel.map(s=>s.verdict).join(','), sel.length);"
failed,failed,failed 3

$ node -e "const a=require('./integrations/pi/validation/evidence/2026-09-14-real-model/run-2/archive.json');
  console.log(a.samples.reduce((n,s)=>n+s.tokens.total,0)+a.records.reduce((n,s)=>n+s.tokens.total,0));"
2062826
```

第一个数字对应第 1 节表里 `write-tool-gate-on-recorded-regress` run 2 baseline 的 0/3；第二个对应第 5 节 run 2 的 2,062,826 token。

**原始回放包没有进仓库。** 每个样本的完整工具轨迹是 65KB–1.3MB（30 个样本约 13MB），进仓库只会淹没 diff。仓库里放的是每个样本的数字与模型最终答案；完整包留在本地 `integrations/pi/artifacts/`（已 gitignore），并由 `npm run summarize` 可重新生成同样的数字。

## 9. 复现步骤（含凭证纪律）

```powershell
# 凭证只放在进程环境变量里（本仓库不写任何凭证值）
$env:API_URL = '<OpenAI 兼容网关的 base url>'
$env:API_KEY = '<网关密钥>'

# 免费自检：引用是否过期、provider/model 能否解析、网关目录里有没有这个模型
pwsh integrations/pi/scripts/real-model-run.ps1 -CheckOnly

# 一轮完整运行：check -> 录制 -> 免费预估 -> 确认在声明的上限内 -> 回归（硬停）
pwsh integrations/pi/scripts/real-model-run.ps1 -Samples 3 -BudgetModels 400 -BudgetCostUsd 2.0
```

缺 `API_URL` 或 `API_KEY` 时脚本在任何真实调用之前停下并报错，**不会退化成离线模型**；真实调用不进 `scripts/test.ps1`、不进 CI，也不作为任何隐式步骤。

## 10. 据此的推断（与事实分开写）

1. 这份任务集**确实能区分两个条件**：严格判据下 baseline 9/30、grounded 21/30，差异集中在引用链（`bad_evidence` 15 次 vs 4 次）。三个任务在两轮里都稳定失败，两个任务出现真实的「严格失败 → 严格通过」翻转。
2. 但「任务足够难」这个结论**不成立**：baseline 在内容层面答对了 30 个样本里的 24 个（9 个 `pass` + 15 个 `bad_evidence`），真正的判断错误只有 4 次，而且全部是 JSON 数值类型（`"2"` vs `2`）。**不得把这份结果读成「baseline 答不出来」。**
3. 与第 1 轮「任务太简单」相比，这一轮的可区分性来自**证据链要求**，不是来自任务本身的推理难度。下一步要更硬的失败，得从「需要多跳推理、且答错时不会被格式掩盖」的任务下手，而不是继续增加引用的条数。
4. 两个任务（`cause-precedence`、`pi-tape-mismatch`）在轮次之间不稳定，说明每条件 3 个样本**不足以**给单个任务下结论；本报告因此只对「两轮都达到 ≥2/3 失败」的三个任务写「稳定失败」。

## 11. 限制（本轮没有验证的事）

* 只有一个模型、一个网关、一个 Agent（pi 只读调查）。结论不外推到其他模型或更大的样本。
* `snapshot` 模式只适用于只读工具；有副作用的工具不在此列。
* 任务集测的是**代码调查**这一种任务形态；编辑文件、执行命令、多轮对话都没有接入。
* 上限演示用的是 `--phase regress`（回归阶段单独跑），因此它复用已有录制、只花了 4 次调用；`--phase all` 的完整流程里有免费的预估把关，超限时**不会启动**回归（把格子标成 `not_started`），两种行为都在 `src/validate.ts` 里，也都有测试。
* 网关自报的 `cost` 字段只在一次直连探测里观察到（值为 0）；没有网关账单可以对账，因此本轮的成本结论只能是「按本地价目折算的估算」。

