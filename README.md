# Agent Flight Recorder

**Replay, debug and evaluate AI agents like software.**

当前重点：面向工具型 Agent 的本地回归调试器。固定历史工具证据，重跑模型决策，将失败变成可执行测试。

新增 [pi 只读代码调查运行器](integrations/pi/README.md)：使用真实 pi coding-agent SDK，支持本地录制包、全程复现、回归与用例。它是独立 TypeScript 适配器，不由 Python 服务端启动，也不提供任意步骤恢复。LangGraph 示例仍用于离线演示。

[![CI](https://github.com/wynxing/agent-flight-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/wynxing/agent-flight-recorder/actions/workflows/ci.yml)

AI Agent 的黑匣子与可回放调试平台。它要回答的问题不是"如何构建一个 Agent"，而是：

> 当 Agent 执行错误、结果异常或行为不可解释时，如何知道它到底发生了什么，并能够复现、验证和修复问题。

传统软件可以靠日志、断点和单元测试定位问题，但 Agent 的执行链路涉及模型、上下文、外部工具与环境状态，
同一个输入不保证同一个输出。这个项目要补上的就是这块：录制受支持的模型与工具边界，在明确的适配范围内重跑，
并把生产环境里的失败变成可以反复验证的回归用例。

## 快速开始

前置条件：Python 3.11 以上、[uv](https://docs.astral.sh/uv/)、Node.js 20 以上。

```powershell
pwsh scripts/dev.ps1
```

打开 http://127.0.0.1:5273 就能看到控制台。首次启动会自动跑一次示例 Agent 落库，并把它自带的那条回归用例跑成一个失败，
所以开箱就有完整演示数据：一条运行记录 + 一条红色的用例，不用先自己去造失败样本。
不带前端开发服务器时，`pwsh scripts/dev.ps1 -NoWeb` 会用服务端托管的已构建页面，地址是 http://127.0.0.1:7710 。

跑一遍完整验证：

```powershell
pwsh scripts/test.ps1
```

这条命令**不需要手工前置**：脚本会自己补上缺失的依赖（Python 工作区、`web/node_modules`、
`integrations/pi/node_modules`），并在输出里说明补了什么，所以全新克隆直接跑即可。
依赖引导排在所有检查之前，因此 pytest 里的跨语言契约测试不会因为「跑它的时候 pi 依赖还没装」而被跳过。

结论分三态，退出码与之一致：`全部验证通过` / `通过，但有未验证项`（会列出没跑到的检查）/ `失败`。
只要存在任何未验证项，脚本就不会输出「全部通过」。未验证项有两个来源：脚本自己的判断（依赖缺失或被
显式排除），以及被调用工具的自我申报——pytest 的跳过同时从 junit 报告（结构化的计数与明细）与
`-rs` 汇总（明细，留在日志里供人核对）两个来源确认，任一来源缺失或两者对不上都算未验证；
pi 运行器的 skipped / todo 计数同样登记。所以「某个测试自己跳过了」不会被写成「全部验证通过」。

加 `-Strict` 时任何未验证项都判为失败——CI 用的就是它，因此「CI 绿」等价于
「Python、控制台与 pi 运行器都真的跑过」。只想跑 Python 用 `pwsh scripts/test.ps1 -PythonOnly`
（控制台与 pi 会被登记为未验证项，所以它和 `-Strict` 组合会失败）；
`-SkipInstall` 会关闭依赖引导，把缺失或损坏的依赖记为未验证项。

## 五分钟走完一遍闭环

整个项目的价值都压在这一条路径上：**失败运行 → 时间线复盘 → 从某一步回放 → 改 Prompt 重跑 → 对比定位分叉 → 沉淀为用例**。

**1. 打开 seed 运行。** 在「运行记录」里点开最下面那条带「示例数据」标记的运行。它是 mock 的 SRE 调查场景：
checkout-api 的 p99 延迟在 14:02 抬升，日志里全是 Redis 连接池报错，而同期有一次部署把 `REDIS_POOL_SIZE` 从 64 改成了 8。

**2. 看结论。** 右侧「本次结论」写着根因是 Redis 连接池耗尽。这是错的：连接池报错是那次部署造成的**结果**，不是原因。
Agent 看到了全部证据，但把症状当成了根因。

**3. 从第 15 步回放。** 在时间线上把鼠标移到第 15 步（那次模型调用），点「从这里回放」，右侧面板会跳到该步。
模式选「复现」，点「发起回放」。新运行会出现在运行记录里，结论与原运行逐字一致，复现模式的确定性就体现在这里。

**4. 换 Prompt 重跑。** 回到 seed 运行，模式选「回归」，Prompt 版本选 `grounded`。
新版本的 Prompt 要求把指标拐点与部署时间对齐，并检查该部署改动的配置项。回放后结论变成"v2.4.1 引入的连接池配置回归"，并建议回滚 `REDIS_POOL_SIZE`。

**5. 看差异。** 打开「运行对比」，基准选 seed 运行，对照选刚跑出来的回放。
页面会指出第一个不同的步骤是"模型输出变化"，并显示工具调用序列完全一致。差异只在推理上，这正是换 Prompt 的预期效果。

**6. 看用例红绿翻转。** 打开「回归用例」，里面已经有一条播种时就跑过的用例：「根因必须指向 REDIS_POOL_SIZE 配置回归」，
状态是**失败**——它断言结论必须指向配置回归，而默认 Prompt 跑出来的结论确实没有。
在「以哪个 Prompt 运行」里选 `grounded`，点「运行用例」，同一条断言就通过了。同一条断言，两种 Prompt，明确的通过或失败。

用例不是手搓的：它由 Agent 自己在 `AgentSpec.seed_cases` 里声明（见下方「接入自己的 Agent」），
播种时走的就是界面创建用例的同一条链路。想自己建一条也可以在运行详情页右侧用「创建用例」。

离线演示默认使用脚本化模型（`afr-scripted-sre-v1`），界面上会标注它是示例数据。这不是假装成真实模型的输出：
剧本的分支由 system prompt 决定，因此"改 Prompt 导致结论改变"是真的发生了的行为差异。配置 `OPENAI_API_KEY` 后
可以把回归模式的模型换成真实模型（见下方「接入真实模型」）。

## 它是怎么工作的

**接入方式是一行。** 给你的 LangChain / LangGraph Agent 加一个中间件即可：

```python
from agent_flight_recorder import Recorder, SideEffect
from agent_flight_recorder.middleware import FlightRecorderMiddleware

recorder = Recorder("my-agent", model="gpt-5")
recorder.start(task="调查 checkout-api 延迟异常")

agent = create_agent(
    model=model,
    tools=my_tools,
    middleware=[FlightRecorderMiddleware(recorder, tool_side_effects={
        "kubectl_apply": SideEffect.WRITE,
        "notify_oncall": SideEffect.EXTERNAL,
    })],
)
result = agent.invoke({"messages": [HumanMessage(content="...")]})

recorder.finish(result=result["messages"][-1].content)
recorder.close()   # 短生命周期脚本必须显式关闭，否则缓冲区里的事件会丢
```

录制链路永远不向上抛异常。上报失败只会记在 `recorder.stats` 里，不会打断或拖慢被观测的 Agent。
想确认链路真的通，用一次真实往返自检，而不是检查配置文件是否存在：

```python
from agent_flight_recorder import doctor
print(doctor("http://127.0.0.1:7710").as_dict())
```

**回放引擎在 SDK 里，不在服务端。** 它由框架无关的核心（策略解析、效果匹配、分叉判定）加一层框架适配组成。
本地 MVP 里由服务端作为宿主来执行，这只是部署选择：你完全可以在自己的进程里加载历史运行并回放。

## 回放语义

回放不是一个动作，是两件不同的事，混在一起会让成本、可信度和界面全部失焦。

| 模式 | 模型 | 工具 | 回答的问题 |
| --- | --- | --- | --- |
| **复现** | 用录制结果 | 用录制结果 | 受支持的行为轨迹能否一致重放？ |
| **回归** | 真实执行 | 用录制结果 | 改完到底有没有变好？ |

回归模式下工具沿用录制结果不是偷懒：要比较的是 Agent 的推理与决策，如果工具数据同时漂移，两次运行就不可比了。

每一步实际怎么执行，按固定优先级解析：

```
by_seq[seq]  >  by_kind[kind]  >  default  >  "recorded"
```

单步覆盖永远赢，所以"只让第 7 步真实调模型"是一等能力，不是特例代码。
LangGraph 分叉点之前的模型与工具调用使用录制结果；普通节点代码仍可能重新执行，不等于 checkpoint 恢复。pi 第一轮只支持从任务起点回放。

### 副作用安全

回放一个 SRE Agent 时，它可能再次删除 Pod、再次发出工单。只读 trace 的可观测性工具不需要回答这个问题，
因为它们是只读的；本平台会重新执行，所以必须回答。

每个工具声明副作用等级，回放中请求真实执行的 `write` 与 `external` 默认被降级为 dry_run，返回"本应做什么"的合成结果；读取历史结果不触发副作用闸门。
要真实执行必须同时满足：回放策略显式为 live **且** 打开了 `allow_side_effect_execution`，执行后还会在时间线上留下高可见度的告警标记。
默认拒绝，显式放行，留痕可查。

### 回放预算

回放可以声明硬上限：最大成本（USD）与最大模型调用次数。上限在 **SDK 层**生效——达到任一上限时引擎在步边界停止，不再发起新的模型调用；已经发出的那次调用允许跑完并如实记账。

* **事先可预估**：发起前可以先看「预计几次真实模型调用、大约多少成本」。预估不调用任何模型、也不创建 Run；父 Run 缺少可用 token 记录或模型不在价格表内时，如实显示「无法预估」，而不是给一个编造的数字。
* **超限不是失败**：因预算停止的回放判为 `inconclusive`（成因码 `budget_exceeded`），Run 状态是「已中止」，控制台明确显示为「因预算停止」而不是普通失败。
* **事后如实记账**：Run 元数据里有「已用 / 上限 / 是否触顶」的对照；成本未知时是「未知」而不是 0。
* **不设上限时行为不变**：两个维度默认都不声明，回放行为与没有这套能力时一致；复现模式本来就不花模型钱，声明了上限也不会被预算逻辑误判。
* **整批也声明得上限**：一次批量套件（用例 × 条件）可以在提交时声明这一批最多花多少。
  到点之后不再启动新格子，已启动的格子跑完并如实记账；没轮到的格子标成「未启动（因批次预算用尽）」，
  既不判失败、也不算「拿不到结论」。见 [回放语义](docs/replay-semantics.md) 第 10 节。
* **pi 侧同名同义**：`integrations/pi` 的批量预算（`--budget-models` / `--budget-cost-usd`）用的是
  同一套字段名与同一条判定（步边界硬停、`stopped_by` 取值、成本未知不是 0），触顶同样落
  `inconclusive` + `budget_exceeded` + `aborted`，没轮到的样本记 `not_started`。只在真的声明了
  上限时启用：**不声明预算的执行行为与加这套能力之前逐字一致**（运行器自己的 `--max-models` 等
  安全上限仍走原来的执行错误路径）。见 [回放语义](docs/replay-semantics.md) 第 9.7 节。

### 平台做不到什么

- **上游漂移**：provider 会下线模型版本、alias 会指向新权重，"精确复现"在 live 路径下物理上不可能。要精确复现就用复现模式。
- **时间的不可复现**：依赖"当前时间"的工具在回放中会得到不同结果，除非工具自身支持注入时钟。
- **外部状态漂移**：即便工具结果是录制的，外部世界已经改变，因此 dry_run 的"本应做什么"不等于"当时做了什么"。
- **非确定性推理**：同一输入不保证同一输出，这正是需要多次采样与断言、而不是逐字节 diff 的原因。

## 仓库结构

| 目录 | 内容 |
| --- | --- |
| `sdk/` | Python SDK：录制器、LangChain 中间件、回放引擎、OTel 导出 |
| `server/` | FastAPI 服务端：入库、脱敏、回放调度、Diff、用例执行 |
| `web/` | Vue 3 控制台：运行记录、时间线、回放面板、差异对比、回归用例 |
| `examples/langgraph_sre_agent/` | 示例 Agent，同时也是回放能力的真实被测对象 |
| `docs/` | 见下方「文档」一节 |
| `scripts/` | `dev.ps1` 与 `test.ps1` |
| `integrations/pi/` | pi SDK 本地运行器、固定提交调查任务、跨语言协议测试 |

数据存放在 `data/afr.db`（SQLite 单文件，已加入 .gitignore）。删掉它就回到全新状态，下次启动会重新播种
——包括那条自带的用例，它会重新被建出来并跑成一个失败。

## 接入自己的 Agent

平台不硬编码任何 Agent。它通过 entry point 组 `afr.agents` 发现"怎么重建这个 Agent"，因此回放能力可以脱离示例独立使用：

```toml
[project.entry-points."afr.agents"]
my-agent = "my_package.agent:agent_spec"
```

```python
from agent_flight_recorder import AgentSpec, Recorder, SeedCase, SideEffect

def build_my_agent(*, model=None, system_prompt=None, middleware=(), checkpointer=None):
    """回放引擎约定的工厂契约。为 None 表示沿用默认值。"""
    ...

def agent_spec() -> AgentSpec:
    return AgentSpec(
        name="my-agent",
        build=build_my_agent,
        seed=lambda recorder: run_once(recorder),   # 可选：首次启动时播种
        tool_side_effects={"notify_oncall": SideEffect.EXTERNAL},
        prompt_presets={"default": PROMPT_V1, "grounded": PROMPT_V2},  # 可选：界面里可切换
        seed_cases=[                                # 可选：播种时连用例一起建，并立刻跑一次
            SeedCase(
                name="根因必须指向那次配置回归",
                from_seq=15,
                assertions=[{"type": "final_output_contains", "value": "REDIS_POOL_SIZE"}],
            )
        ],
    )
```

没有注册的 Agent 依然可以正常录制与查看，只是服务端无法替你发起回放。

### 接入真实模型

```powershell
$env:OPENAI_API_KEY = "..."
cd examples\langgraph_sre_agent
uv run python -m sre_agent.run --model gpt-5-mini --prompt grounded
```

同一个标识也可以直接填进控制台的「模型覆盖」，用于回归模式。

### 真实模型的失败任务集（会花钱，手动跑）

`integrations/pi` 里还有一套**代码调查**回归：固定提交 + 只读工具，模型必须给出结构化答案与准确
源码引用。`validation/failure-suite.json` 是其中 baseline 真的会失败的那一套任务，用来回答
「改完到底有没有变好」。

```powershell
$env:API_URL = 'https://<网关>/v1'   # 只写变量名，值不进仓库
$env:API_KEY = '<网关密钥>'
pwsh integrations/pi/scripts/real-model-run.ps1 -Samples 3
```

脚本按「免费自检 → 录制 → 免费预估 → 确认在声明的上限内 → 回归」的顺序跑；缺 `API_URL` 或
`API_KEY` 时在任何真实调用之前停下并报错，不会退化成离线模型，也不会把凭证写进任何文件。
**真实调用不进 `scripts/test.ps1`、不进 CI，也不作为任何隐式步骤。** 流程、上限与结果归档见
[真实失败任务集](docs/pi-failure-suite.md)。

## 用例与断言

只有确定性断言。引入 LLM Judge 会带进第二个不确定性来源，而当前要证明的是"失败案例可以被稳定复现与验证"。
评测结论区分 `passed / failed / inconclusive / error`。缺失录制、脱敏或不支持的上下文会阻止可信判断，不应当作断言失败，更不能判通过。行为首次分叉仅表示变化，不自动证明错误或根因。
一条用例包含：源运行、回放起点、模式、断言集，以及条件标签（模型、Prompt 版本）。

回放拿不到可信结论时不会静默合成：`ReplayResult` 带 `complete` 与结构化成因 `reason`（`code` + `detail`），
`ReplayExhaustedError` 是公开的失败语义（见 [回放语义](docs/replay-semantics.md) 第 1、8 节）。
录制边界缺失、`seq` 缺口、脱敏、录制丢失、上下文截断与初始状态未恢复都会走到这条路径，
各自给出不同的成因码。

「为什么无法判断」是一等判定：成因是一个**跨语言共享的闭集**（Python `InconclusiveCode` 与 pi 的
TypeScript 联合类型取值逐字一致），码与说明分离，服务端复用同一份分类，控制台按成因给出中文、
可操作的说明。**副作用被闸门拦截**与**录制不完整**是两种不同的 inconclusive，码不同、下一步也不同：
前者说明这次执行本来就没有真实发生，后者说明拿不到可信结论。历史数据里的自由文本 `reason` 会被兼容解析，
认不出的取值落到「未知成因」并保留原文。

支持的断言类型：`no_error`、`final_output_contains`、`final_output_not_contains`、`final_output_matches`、
`tool_called`（可带参数匹配）、`tool_not_called`、`tool_sequence_equals`、`max_tool_calls`。

### 批量运行与条件矩阵

用例页可以勾选多条（或一键全选）一次发起，并给出多个条件（Prompt 版本、模型），
形成「用例 × 条件」的结果矩阵。每个格子的结论都记着它属于哪个条件，
因此同一条用例在两个 Prompt 版本下的结论分别可辨认，不会出现「这条用例上次是什么条件下的结论」这种歧义。

汇总**按条件分组呈现，不合成总分**：每个条件各自给出四态计数与可判断率，
并明确写出「N 条中 M 条拿不到结论」。可判断率的分母永远是该条件自己的用例数，样本量一并给出；
没有任何跨条件的百分制、加权分或单一总分（见 [PRD 6.5](docs/PRD.md) 与 [上报协议](docs/protocol.md) 第 7 节）。
`inconclusive` 与 `failed` 在聚合层依然分列：前者是「拿不到结论」，后者才是「结论是不通过」，
两类的成因都能在批次结果里逐条展开。

**「还没跑」不等于「拿不到结论」。** 未完成的格子从未执行过，没有任何结论，因此它与
「无法判断」分开计数（`total = 可判断 + 拿不到结论 + 未完成`）。批次进行中，卡片只陈述
「已完成 / 未完成」；只有整批跑完，才会说出「N 条中 M 条拿不到结论」——那句话此时才有证据。

**整批的预算上限。** 提交这一批时可以声明它最多花多少（最大成本 / 最大模型调用次数，与单次回放
同一套语义）；不声明就是与没有这套能力时逐字一致。声明之后整批按顺序逐格执行：到点不再启动新格子，
已启动的格子跑完并如实记账，没轮到的格子状态是 `not_started`，在汇总与下钻里显示为
**「未启动（因批次预算用尽）」**——它不是 `failed`，也不是「跑过了但拿不到结论」，而是归在
「未完成」一侧（`未完成 = 排队中 + 正在执行 + 未启动`）。因此「一张卡片上每个短语只对应一个数」
这条仍然成立。

提交前还可以先预估整批：`POST /v1/suites/estimate` 只读地给出预计的调用次数与成本；
只要有一格给不出成本，整批就是「无法预估」+ 原因，**不把已知的那些加起来冒充整批成本**。
声明了上限时，预估还会说清这个上限到底约束了什么：调用次数上限是硬的；成本上限在成本
算不出来时**根本不参与判定**（未知不会被当成 0），算得出来时也允许已发出的那次调用跑完。

数据契约：`POST /v1/suites` 立即返回批次标识，执行在后台；
`GET /v1/suites/{id}` 返回总数、已完成数、按条件分组的汇总，以及整批的预算记账
（已用 / 上限 / 是否触顶 / 有多少格子因此没跑）。单条运行路径 `POST /v1/cases/{id}/run` 行为不变。

## 文档

| 文档 | 内容 | 适合谁读 |
| --- | --- | --- |
| [产品需求文档](docs/PRD.md) | 问题定义、目标用户、用户旅程、能力范围、关键产品决策、成功标准、风险与路线 | 想知道"这东西解决什么问题、边界在哪"的人 |
| [架构设计](docs/architecture.md) | 系统总览、分层与依赖方向、核心实体、关键数据流、设计决策与理由、并发模型、存储设计、扩展点、技术债 | 要读代码、接手维护或扩展它的人 |
| [上报协议](docs/protocol.md) | SDK 与平台之间的数据契约（Run / Event / EffectPolicy / 幂等与续传 / 脱敏 / 版本策略） | 要写新语言 SDK 或对接上报的人 |
| [回放语义](docs/replay-semantics.md) | 复现与回归的定义、策略优先级、副作用安全、确定性边界、分叉检测 | 要用回放能力、或想确认它到底能保证什么的人 |

如果你只有五分钟，读 PRD 的第 1、2、6 节；如果要动手改代码，读架构设计的第 2、4、5 节。

## 上报协议

`POST /v1/ingest`，载荷为 `{protocol_version, sdk, run, events[]}`，按 `(run.id, event.seq)` 幂等。
批次可重发、可续传，`run` 头每个批次都完整携带。完整定义见 [docs/protocol.md](docs/protocol.md)。

协议先立住、SDK 后补：新增一门语言的 SDK 只是新增一个客户端，服务端不需要改。本轮的 TypeScript SDK 不在范围内。

控制台的时间线通过 SSE 实时尾随正在执行的运行（`GET /v1/runs/{id}/events/stream`）。

## 边界

- 单用户、本地、无鉴权，只监听 localhost。没有 Docker，没有多租户字段。
- 脱敏在服务端入库时执行，因此 SDK 本地缓冲区里仍是明文；生产部署应把上报端点视为可信边界。
- 成本与 token 为估算值，未匹配到价格表的模型返回"未知"而不是 0。
  回放预算沿用同一条立场：成本未知时是「未知」而不是 0；那一轮成本上限无法判定，调用次数上限仍然生效。
- 整批的预算是**单批**的上限：不做跨批次的预算池 / 配额系统。成本未知时整批的成本上限同样无法判定，
  调用次数上限仍然按次数硬停；「成本未知」也不会被当成 0 说成「没花钱」。
- 不含 LLM Judge、数据集版本管理、跨批次的历史趋势看板；这些是下一阶段的内容。单批内的条件矩阵与
  按条件分组的汇总已可用，且刻意不提供任何跨条件的合成总分。
- OTel GenAI 语义约定只做导出（`GET /v1/runs/{id}/otel`），不做导入。
