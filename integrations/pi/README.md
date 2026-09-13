# pi 本地回归调试运行器

通过 pi coding-agent SDK 调查固定提交的代码；模型与工具结果保存为本地 JSON 回放包，之后上报 Msee。服务端不运行 Node，不需要修改 pi 上游。

## 安装

需要 Node.js >=22.19、Git、PATH 中的 ripgrep (`rg`)。在本目录执行：

```powershell
npm ci --ignore-scripts
npm run typecheck
npm test
```

固定 `@earendil-works/pi-coding-agent` / `pi-ai` 0.85.1 和锁文件。接口核对基准为上游提交 `71dca871bc80b6bc97be37f0ca3189399d651fff`。不自动下载工具；find 使用 Git 跟踪文件索引，不依赖 fd。

## 录制、重放、用例

先创建 task.txt 和 prompt.txt，模型必须显式给出 provider/model。原生 provider 可使用其环境变量；也可通过 `--auth-path` 指向 pi auth.json，`--models-path` 指向静态 models.json。不要把凭据写进任务或 Prompt，也不接受 Shell 形式的配置命令。未指定配置路径时不读取用户的 pi 配置目录。

自定义 OpenAI 兼容网关按 pi 的 `models.json` 格式配置，`apiKey` 用 `$ENV_VAR` 插值，示例见 `validation/opencode.example.json`。运行器会先校验配置文件，拒绝 `!command` 形式的 apiKey/headers，避免配置变成任意命令执行；解析后的密钥不会写入回放包。网关不提供价格时成本记 0，且不参与结论。

```powershell
npm start -- record --repo ../.. --commit da1038c15ad5f010416924270ff4a0e17a15169f --task task.txt --prompt prompt.txt --provider PROVIDER --model MODEL --out artifacts/run.json
npm start -- replay --bundle artifacts/run.json --mode reproduce --out artifacts/reproduce.json
npm start -- replay --bundle artifacts/run.json --mode regress --prompt validation/grounded.txt --out artifacts/regress.json
npm start -- case create --bundle artifacts/run.json --assertions assertions.json --out artifacts/case.json
npm start -- case run --case artifacts/case.json --prompt validation/grounded.txt --out artifacts/case-run.json
```

回归默认继承录制的 provider/model；换模型时必须同时指定这两个选项。复现拒绝 Prompt/model 覆盖。输出文件先落盘，再尝试上报 `http://127.0.0.1:7710`，可用 `--endpoint` 覆盖。上报失败不删除本地文件。

### 回归时工具结果从哪来（`--tool-source`）

| 取值 | 行为 | 何时用 |
| --- | --- | --- |
| `recorded`（默认） | 严格按工具名、参数、次序消费录制结果；不一致就停 | 证据必须逐字节可比的场景 |
| `snapshot` | 只读工具在固定提交的工作树上真实执行 | 调查类 Agent 的日常回归 |

`snapshot` 的合法性来自两个前提：工具集全只读（没有可执行副作用），目标仓库固定在某个提交（证据不会漂移）。因此两个 Prompt 仍然可比，而模型可以自由换查询方式。

这是实测出来的差异，不是设计偏好：同一套 5 个任务 30 个回归样本，`recorded` 下 28 个因为模型换了 grep 参数而无法判断，`snapshot` 下 30 个全部给出明确结论。详见 [docs/pi-validation.md](../../docs/pi-validation.md)。

```powershell
npm start -- replay --bundle artifacts/run.json --mode regress --tool-source snapshot `
  --repo ../.. --prompt validation/grounded.txt --out artifacts/regress.json
```

`snapshot` 需要 `--repo`：运行器会为该提交新建 detached 工作树，跑完检查干净后移除；若工作树被改动则保留路径供排查，不强制删除。复现模式始终使用录制结果，不接受该选项。

`assertions.json` 是非空数组，支持 Python 用例的基本确定性断言类型（不含 args_contains），并增加 `json_claim`：答案与人工核实的源码引用一起匹配。参见 `validation/suite.json`。用例保存源包哈希，源包改变后必须重新创建用例。

判定为失败时，用审计分开"答错"和"没按格式输出"：

```powershell
npm run audit -- --dir artifacts/validation
```

输出把每个样本分成 `pass` / `format_only`（答案和引用都对，只是 JSON 外面还写了说明）/ `bad_evidence`（答案对但引用不符）/ `wrong_answer` / `no_json`。只看通过率会把模型的格式习惯误读成能力问题。

退出码：0 通过/运行成功，1 断言失败，2 无法判断，3 执行错误。CLI 最后一行是 JSON 结果；运行成功不等于调查结论正确，必须运行断言并人工核查必要的引用。

## 支持范围

- 只提供 read、grep、find、ls；工具串行运行，禁用压缩、自动重试、技能和外部扩展发现。find 仅搜索 Git 跟踪文件。
- 录制自动创建 detached 工作树；结束后检查干净再移除。若发生修改则保留路径供排查，不强制删除。
- 工具路径必须在工作树内；拒绝 symlink、junction、路径穿越及直接访问 `.git`（不区分大小写）。是否越界一律按**解析后的真实位置**判定，而不是按路径的写法，因此同一个目录的 8.3 短名、junction 或大小写差异不会被误判为越界；返回值始终是规范化路径。不存在的路径显式报 `Path does not exist`，不会退化成原始 ENOENT。这限定了本运行器工具接口，不是针对其他本地进程的 OS 沙箱；不要并发改动临时工作树。
- 模型看到显式 Prompt 和相对路径。物理临时目录不注入 Prompt，避免不同临时路径形成假分叉。
- 复现重新驱动 pi 循环，用完整原生响应恢复模型与工具结果，检查输入和完整轨迹，不需要原工作树或真实模型凭据。不是 token 流逐帧复现。
- 回归只真实调用模型，按工具名称、参数及调用次序消费旧结果。缺失结果立即停止，返回 `inconclusive`，绝不尝试真实工具回退。
- 默认上限：20 次模型调用、60 次工具调用、10 分钟。可通过 `--max-models / --max-tools / --timeout-ms` 显式调整，SIGINT 取消。
- 所有回放包落盘前脱敏；回放数据发生脱敏时包标记不完整，禁止可信回放。规则无法识别所有企业自定义秘密格式。
- Python 服务端只负责展示、Diff 和入库；pi 用例在本地执行，控制台显示命令入口，不支持单步回放。

## 真实验证

```powershell
npm run validate -- --repo ../.. --provider PROVIDER --model MODEL --out artifacts/validation
```

五个任务的预期答案已按固定提交人工检查；执行前会再次校验引用的原文行。每个任务录制一次，然后 baseline/grounded 各回归三次。`--only <id,...>` 与 `--samples <n>` 可先跑子集，避免在没有信号时跑满矩阵。源码、Prompt、完整包和 JSON/Markdown 报告共同构成证据。

`results.json` 记录通过、失败、无法判断、错误，以及模型调用数、token 与耗时。人工负向控制单列，不参与真实模型统计。准确源码引用是较严格的判定条件，未通过时要区分格式/引用问题与实质判断错误。没有修复现象就报告没有观察到修复；录制覆盖不足不解释为模型退化。

当前执行状态见 [验证记录](../../docs/pi-validation.md)。真实模型请求不会作为 CI 的隐式步骤。
