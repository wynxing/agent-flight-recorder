# 真实失败任务集的原始归档（2026-09-14）

这里是 `docs/pi-failure-suite.md` 里每一个数字的原始记录。它们来自两轮真实的 `deepseek-v4.1-flash` 运行，
模型与工具都真实执行；**不含任何凭证值**（网关地址与密钥来自运行进程的环境变量 `API_URL` / `API_KEY`）。

| 目录 | 是什么 |
| --- | --- |
| `run-1/` | 第一次完整运行（录制与回归分两次执行；那一次的批量账本有缺陷，见报告第 7 节） |
| `run-2/` | 第二次完整运行（`--phase all`，一份账本盖住录制与回归） |
| `cap-demo/` | 上限演示：声明 4 次调用上限，第 4 次之后在步边界硬停，后面的格子 `not_started` |

每个目录里：

* `archive.json` —— 机器可读的原始归档：每个样本的条件、结论（`verdict`）、成因（`cause`）、真实模型调用数、
  token、成本（本地价目折算）、运行标识；加上整批的「声明上限 / 已用 / 是否触顶 / 未启动格子」，以及 5 个录制的用量。
* `summary.json` —— 由 `npm run summarize` 从原始回放包重算出来的同一批数字，另外带上每个样本的模型最终答案
  与内容分类（`pass` / `bad_evidence` / `wrong_answer` / `format_only` / `no_json`）。
* `estimate.json` —— 录制之后、回归之前那次**免费**预估（不调用任何模型）。
* `report.md` —— 运行器自己写的报告。
* `negative-control.json`（只有 run 2）—— 人工把答案改成相反值，判据必须判它失败（证明判据不是恒真）。

## 重算数字

```powershell
cd integrations/pi
# 从原始回放包重算（离线、不花钱）：包在本地 artifacts/ 里，不进仓库
npm run summarize -- --dir artifacts/round8-replication --suite validation/failure-suite.json --out /tmp/summary.json
# 从已进仓库的归档重算合计（不花钱）
pwsh scripts/real-model-run.ps1 -SummaryOnly validation/evidence/2026-09-14-real-model/run-2/archive.json
```

## 没进仓库的东西

每个样本的**完整回放包**（模型与工具的全部轨迹，65KB–1.3MB/样本）留在本地 `integrations/pi/artifacts/round8/`、
`artifacts/round8-replication/`、`artifacts/cap-demo/`（该目录已 gitignore）。仓库里放的是这些包推导出的数字与答案，
以及可以重新算出它们的命令；包本身进仓库只会淹没 diff。

