# Agent Flight Recorder 架构设计

| 项 | 值 |
| --- | --- |
| 版本 | v1.0 |
| 状态 | 与当前代码一致；本文档描述**已实现**的结构 |
| 相关文档 | [PRD](PRD.md) · [上报协议](protocol.md) · [回放语义](replay-semantics.md) |

本文档回答的是"系统为什么这样切分"，而不是逐文件说明代码。
凡是涉及取舍的地方，都写了理由和代价，方便后续的人判断该不该推翻它。

---

## 1. 系统总览

新增 `integrations/pi/` 专用 TypeScript 运行器，使用 pi coding-agent SDK，在独立工作树录制只读调查。
本地回放包先脱敏落盘，再调用现有 ingest；Python 服务端不加载或执行 Node。pi 用例也在本地执行。
`run.metadata.runtime = "pi"` 控制界面能力；`afr_replay.complete/reason/verdict` 表达完整性与结论。
Run 状态保持原有枚举，用例状态增加 `inconclusive`。SQLite 字符串列无需迁移，旧记录无 complete 字段时按旧显示处理。

详见 [pi 运行器](../integrations/pi/README.md)。

```
[被测 Agent 进程]
  LangGraph / LangChain Agent
    -> FlightRecorderMiddleware      拦截每一次模型调用与工具调用
       -> Recorder                   内存缓冲；录制路径永不抛异常
          -> 后台线程批量上报
             |
             |  POST /v1/ingest       幂等键 (run.id, event.seq)
             v
[afr-server . FastAPI]
  ingest    -> 脱敏 -> SQLite (runs / events / cases)
  replay    -> 调用 SDK 回放引擎 -> 回放结果作为新 Run 入库
  diff      -> 对齐两个 Run 的行为步骤，产出差异视图
  cases     -> 断言求值，产出 pass / fail
             |
             |  REST + SSE
             v
[afr-console . Vue 3]
```

三个值得注意的地方：

1. **回放引擎不在服务端，在 SDK 里。** 服务端只是本地 MVP 选的宿主。
2. **录制是单向的。** Agent 进程只需要能发 HTTP，不需要知道平台的存在。
3. **回放产生的是新 Run，不是对旧 Run 的修改。** 父子关系靠 `parent_run_id` 表达，因此两者天然可比。

---

## 2. 分层与依赖方向

```
examples/langgraph_sre_agent      示例 Agent + 平台可发现的注册信息
        |
        v
server/afr_server                 FastAPI、存储、调度、差异、断言
        |
        v
sdk/agent_flight_recorder         录制器、回放引擎、协议模型

web/                              独立前端，只通过 HTTP 契约耦合
```

**依赖是单向的：`server` 依赖 `sdk`，`sdk` 不依赖 `server`。**

这条约束是刻意的，它保证 SDK 能被单独装进任何 Agent 进程，不需要为了录制而拖进一个 Web 框架。
`sdk` 的核心包只依赖 `pydantic`；LangChain / LangGraph 相关代码全部走惰性导入，因此不用框架的用户也能使用录制器与回放引擎。

---

## 3. 核心实体

```
Run  ──1:N──> Event            append-only，seq 单调递增
 │
 │ parent_run_id（自引用，指向被回放的运行）
 v
Run（回放）

Case ──N:1──> Run（来源）       带断言集 + 回放配置
Diff                            两个 Run 的计算视图，不持久化
```

| 实体 | 定义位置 | 说明 |
| --- | --- | --- |
| `Run` | `sdk/.../models.py` `RunRecord` | 一次执行。回放时携带 `parent_run_id` / `replay_from_seq` / `effect_policy` |
| `Event` | `sdk/.../models.py` `Event` | 追加写入，写入后不再修改。类型见协议文档 |
| `Case` | `server/.../tables.py` `CaseTable` | 源运行 + 起点 + 模式 + 断言集 + 条件标签 |
| `Diff` | `server/.../diff.py` `RunDiff` | 纯计算，按需生成（当前未做缓存） |

**回放不是独立的表。** 一个有 `parent_run_id` 的 Run 就是回放，这样父子血缘、列表筛选、对比都不需要额外机制。

---

## 4. 关键数据流

### 4.1 录制

```
Agent 执行
  -> 中间件 wrap_model_call / wrap_tool_call 拦截
     -> 组装 Event（输入、输出、token、耗时、副作用等级、结果来源）
        -> Recorder.record()：加锁 append 到内存缓冲
           -> 后台守护线程按周期批量 drain
              -> HttpTransport 发送 POST /v1/ingest
                 -> 服务端脱敏 -> upsert Run -> 按 seq 去重插入 Event -> 重算汇总
```

录制路径只做内存追加（一次加锁的 list append），网络与序列化全部在后台线程。
任何异常都被吞掉并记入 `RecorderStats`，**绝不向上传播**。代价是短生命周期脚本必须显式 `close()`，否则缓冲区里的内容会随进程退出丢失。

### 4.2 回放

```
POST /v1/runs/{id}/replay
  -> 校验：Run 存在、起点不越界、Agent 已注册
     -> 先把回放 Run 的行建好（避免用户点开链接看到 404）
        -> 提交到后台线程池，立即返回新 run_id
           -> 加载父 Run 的完整事件流
              -> ReplaySession 建立"行为步骤"序列与录制效果库
                 -> ReplayMiddleware 按策略接管每一步
                    -> 分叉点之前：强制用录制结果，不产生任何真实调用
                    -> 分叉点之后：按 by_seq > by_kind > default 解析
                       -> 每步与父 Run 对应步骤比较，在线记录首个分叉
                          -> 结果作为新 Run 写回数据库
```

### 4.3 差异计算

```
GET /v1/diff?a=&b=
  -> 各自加载事件流
     -> 工具调用按名称序列做 difflib 对齐 -> same / changed / only_a / only_b
     -> 模型调用同样对齐，比较文本与工具选择
     -> 汇总指标逐项求差（错误数、token、耗时、成本）
     -> detect_fork 找第一个行为不同的步骤
```

### 4.4 用例执行

```
POST /v1/cases/{id}/run
  -> 读取用例快照（源运行、起点、模式、断言、条件标签）
     -> 组装 ReplayPlan -> 建好回放 Run 行 -> 后台执行
        -> 回放完成后取出该 Run 的事件
           -> 逐条求值确定性断言 -> passed / failed 写回用例
```

给了新的 system prompt 却没有指定模式时，会自动按**回归模式**执行：
复现模式完全使用录制结果，模型根本不会跑，换 Prompt 也就没有任何意义。

---

## 5. 关键设计决策

### 5.1 摘要

| # | 决策 | 理由一句话 |
| --- | --- | --- |
| D1 | 回放引擎放在 SDK 内，不放服务端 | 回放能力不该依赖平台存在 |
| D2 | 单步策略优先级固定为 `by_seq > by_kind > default` | 让"只重跑第 7 步"成为一等能力 |
| D3 | Checkpoint 由事件日志推导，不做独立机制 | 避免两套状态模型并存 |
| D4 | 副作用闸门默认拒绝，放行需显式开关 | 回放会真实执行，必须默认安全 |
| D5 | 参与对齐的只有模型调用与工具调用 | 错误事件占位会导致步骤整体错位 |
| D6 | 脱敏在服务端入库时执行 | SDK 侧不做，保持录制路径零成本 |
| D7 | 录制链路吞异常 + 显式 flush | 录制失败不允许影响业务 |
| D8 | Agent 通过 entry point 注册 | 平台不硬编码任何具体 Agent |
| D9 | 上报幂等键 `(run_id, seq)` | 批次可重发、可续传 |
| D10 | 回放 Run 先建行，再执行 | 回放是立刻可见的对象 |
| D11 | Diff 忽略 `effect_source` | 否则每次回放对比全屏飘红 |
| D12 | 「无法判断」的成因是跨层共享的闭集，码与说明分离 | 散文不能当码，三层不能各写各的名字 |
| D13 | 副作用被拦与录制不完整给不同的 code | 一个去看录制质量，一个去看副作用策略 |
| D14 | 成因归一化只做一次，且在服务端 | 控制台不再自建第二份解析与第三张码表 |

### 5.2 需要展开的几条

**D5 为什么错误事件不参与步骤对齐**

这是一个在实现过程中真实踩过的坑。最初把 `error` 也当作行为步骤，结果是：父 Run 里的一个错误事件占了位置，
但回放过程不会为 `error` 推进游标，后续步骤整体错位，"第一个不同的步骤"随之失去意义。
现在只有 `model_call` 与 `tool_call` 参与对齐；工具执行失败仍然会记录在 `tool_call` 的 `error` 字段里，信息不丢。

**D12 / D13 为什么成因要跨层共享，且副作用拦截要单独给码**

`inconclusive` 曾经只是四个字加一段自由文本：`reason` 里既有 `recording_loss` 这样的码，
也有 `_no_recording_text(...)` 生成的整句英文。同一个概念在 SDK（`recording_loss`）、
服务端（自造的 `replay_recording_loss`）与 pi（`no_recording` / `model_output_truncated`）
各有一套写法，调用方无法稳定判定，控制台也只能原样打印。

现在成因是一个**闭集**（`InconclusiveCode`），码与说明分离（`code` + `detail`），
Python 与 TypeScript 的取值集合逐字一致并由测试守着。副作用被拦截与录制不完整是两种完全
不同的情况：前者说明「这次执行没有真实发生」（去看副作用策略），后者说明「拿不到可信结论」
（去看录制质量）。把两者合并成同一个 inconclusive，等于让用户猜该看哪一边。

**D11 为什么 Diff 不看 `effect_source`**

父 Run 是真实执行、回放是录制复现，这是回放的定义而不是行为变化。
把它算作差异会让每一次回放对比都全屏标红，真正的行为差异反而被淹没。
两侧的来源仍然随载荷返回，界面单独标注。

**D10 为什么回放 Run 要先建行**

后台线程要等第一批事件入库才会创建 Run，这中间有一段窗口期。
用户点开刚拿到的链接会看到 404，而这个 Run 在语义上已经存在了。现在改成发起时就建行，状态为 `running`。

**D4 副作用闸门的具体规则**

```
请求真实执行（mode = live）
  -> 工具声明为 read            -> 放行
  -> 工具声明为 write / external
     -> allow_side_effect_execution = false  -> 降级为 dry_run，写告警标记
     -> allow_side_effect_execution = true   -> 放行，但仍写告警标记
```

闸门只在**工具调用**且**请求真实执行**时介入。复现模式读取录制结果、不产生真实调用，因此不需要被降级。
工具未声明副作用时默认按 `read` 处理，因此声明机制必须便宜到"顺手就写了"（`@afr_tool(SideEffect.WRITE)` 一行）。

---

## 6. 并发模型

| 位置 | 机制 | 说明 |
| --- | --- | --- |
| SDK 录制 | 一把锁保护 `_buffer` 与 `_seq` | 只保护内存追加，不做 I/O |
| SDK 上报 | 单个守护线程 | 周期性 drain + 发送；失败的事件会重新排队 |
| 服务端回放 | `ThreadPoolExecutor(max_workers=2)` | 回放是 CPU 与 token 密集型，限制并发避免互相挤压 |
| 服务端用例 | `ThreadPoolExecutor(max_workers=2)` | 用例执行本质是回放，单独池避免占满回放槽位 |
| 服务端批量套件 | `ThreadPoolExecutor(max_workers=2)` | 未声明整批上限时每格一个任务；声明了整批上限就整批交给一个串行调度（额度要逐格扣） |
| 启动播种 | 登记过的守护线程（`background.start`） | 启动后异步进行，不阻塞服务可用；登记是为了**能被等** |
| SQLite | WAL 模式 | 让"边录制边读时间线"不会互相阻塞 |

**回放不占请求线程。** 提交回放后立即返回 `run_id`，前端通过 SSE 或轮询观察进度。

### 后台工作必须可被看见

「提交即返回」是有代价的：后台工作会活过触发它的那个请求。这份代价换来一条纪律——**任何后台
工作都要能被说清、能被等**，否则它会以一种很难查的方式出错。

最典型的是测试隔离：每个测试用一份新的 SQLite 库，而 engine 是按**当前**设置懒建的
（`db.get_engine`）。一个没被看见的后台线程会在换库之后重建 engine，于是连到**下一个测试**
的库上去写。症状不是断言失败，而是后台日志里的 `no such table: ...`，或者某个测试干等 90s
之后超时——门禁在没有改动任何行为的情况下变红（issue #18 就是这么发作的：播种线程当时是裸
`threading.Thread`，谁都没数到它）。

因此有两条规定：

1. **应用起的后台线程走 `afr_server/background.py` 的 `start()`**，登记在册。语义完全不变
   （daemon、立刻返回、不阻塞启动），变的是 `afr_server.background.join_all()` 能等它；
2. **测试侧的不变量**（`server/tests/conftest.py`）：换库（`db.reset_engine`）之前必须先 drain，
   等不到就**判失败**，并报出「哪个任务、连到了哪个库」。等待集 = 三个线程池的命名计数 +
   登记表里还活着的线程。

同一类问题在别处的现场线索也补齐了：写入失败的后台日志会带上 engine 当时绑定的库名
（`db.bound_db_path()`），因为只有一个 `no such table` 几乎无法定位。

**已知未纳入等待集的后台工作**（如实登记，别让「没人看见」当成「没有」）：

| 路径 | 等待集 | 诊断网 | 守护状态 | 原因 |
| --- | --- | --- | --- | --- |
| SDK 上报线程（`afr-flush-*`） | 不在 | **看得见**（名字是 `afr-*`） | 例外（有意不纳入） | 它是 SDK 自己的生命周期，由 `Recorder.close()` 排空；服务端测试用的播种录制器是 `flush_interval=0`，不起该线程 |
| SSE 事件流（`_event_stream`） | 不在 | **看不见** | 例外（有意不纳入） | 它跑在 Starlette/AnyIO 的线程池里，线程名是 `AnyIO worker thread` / `asyncio-portal-*`（实测：**任何**一次 TestClient 请求都会造出这些名字，所以放宽匹配只会把诊断淹没在框架线程里），名字不归我们管。它只读库；`pytest` 里也没有 SSE 用例（只有控制台在用它） |
| `test_suites.py` 的 `_ImmediateThreads` | 不在（drain 之后的提交会重新包裹） | 看不见（默认线程名） | 例外（有意不纳入），范围边界见下 | 测试自己换上的执行器、自己等它跑完 |
| `test_suites.py` 的 stall worker | **在**（`afr-test-suites-stall-worker`） | 看得见 | 由 `test_suites.py::test_condition_reached_the_latest_run_but_not_a_new_unlabelled_one` 断言它在登记表里（改回裸线程必红） | 它会写库（`run_case_blocking`），所以走 `background.start` 登记，而不是只靠测试自己那次 join 自保 |
| `integrations/pi`（Node 运行器） | 不适用 | 不适用 | 不适用 | 独立进程/事件循环，与 Python 侧的 engine 切换无关 |

**范围边界**（不是缺口，但不写下来就会被读成「全覆盖」）：池那一半是靠包裹 `submit` 得来的，
因此在 `install()` 之前就已经提交出去、还在飞的任务看不见——`ThreadPoolExecutor` 不暴露在飞
任务的句柄，「先包裹、再提交」是这条路的纪律。当前仓库里唯一的换执行器用例
（`test_suites.py` 的 `_ImmediateThreads`）自己在测试里等批次落地；要换执行器的测试请照办：
**换完先让一次 drain（`wait_idle()` / `assert_idle()`）把它认下来，再提交。**

一条纪律：**加了新的后台路径，就同时把它登记进等待集**；确实登记不了（第三方线程）的，写进
上面这张表并写清「诊断网认不认得出它」，别让它隐身。诊断网只认 `afr-` 前缀（`conftest.py`
的 `stray_threads()`），因此**自己起的线程要按 `afr-<用途>` 命名**——名字是它唯一的可见性。

### 这条线上的每一处语义，谁在守

守护集中在 `server/tests/test_background_drain.py`，全部是**确定性反证**：把实现改回旧行为
（或删掉那句收紧），对应测试立刻变红——不是「连跑多次没复现」。每一轮的实际变异命令、输出与
还原都留在那一轮的 PR 里，这里只登记「哪条语义、由谁守」：

| 等待集上的语义 | 守护（删掉它必红的测试） |
| --- | --- |
| 应用起的线程登记在册、可被 join | `test_seed_thread_is_in_the_waiting_set`、`test_drain_waits_for_the_seed_thread` |
| 换库点的拆除必须等它，而不是直接换库 | `test_seeded_client_teardown_waits_for_the_seed_thread` |
| 用例池 / 套件池 / **回放池**都进命名计数 | `test_a_case_pool_task_is_in_the_waiting_set`、`test_a_suite_pool_task_is_in_the_waiting_set`、`test_a_replay_pool_task_is_in_the_waiting_set` |
| drain 开始前重新认一次当前执行器 | `test_a_mid_test_executor_swap_is_tracked_by_the_drain` |
| 等不到就**判失败**（不是留一条警告） | `test_an_unlanded_task_fails_the_swap` |
| 等待期间新起的线程也不能漏 | `test_join_all_waits_for_threads_started_while_waiting` |
| 「登记」与「开跑」之间没有缝（锁内 start） | `test_registration_and_start_have_no_gap` |
| 失败可诊断：报任务名与当时的库名，而不是一个计数 | `test_leftover_work_is_reported_by_name_and_database` |
| 产品行为不变：播种仍然异步、不阻塞服务可用、关掉就不起线程 | `test_seed_thread_is_in_the_waiting_set`、`test_seeding_off_creates_no_seed_thread` |

**本线已知缺口清单：空。** 上面每一条语义都能指出一个会真变红的守护；剩下的不是缺口，而是
上表登记的两个「有意不纳入」例外与一条**范围边界**（都在上面写明了）。

---

## 7. 存储设计

| 表 | 关键字段 | 约束 |
| --- | --- | --- |
| `runs` | `id`, `agent_name`, `status`, `parent_run_id`, `replay_from_seq`, `effect_policy`, `summary`, `event_count` | `parent_run_id` 建索引，便于查血缘 |
| `events` | `id`, `run_id`, `seq`, `type`, `name`, `input`, `output`, `error`, `tokens`, `side_effect`, `effect_source` | **唯一约束 `(run_id, seq)`**，这是幂等性的物理保证 |
| `cases` | `id`, `source_run_id`, `from_seq`, `assertions`, `effect_policy`, `last_status`, `last_results`, `last_cause` | `source_run_id` 建索引 |

`cases.last_cause` 存最近一次结论的结构化成因（`{code, detail}`），契约为「结论不是 passed / failed 时非空」：
只有结论没有成因，用户无从知道该去看录制质量、副作用策略还是执行本身。新增可空列的补齐由 `db._add_missing_columns` 在启动时做（可重入，SQLite 单文件），
因此已有库不需要重建。

**事件表是 append-only 的**：代码里只有插入与存在性检查，没有任何更新路径。
这是"回放可信"的前提：如果历史事件可以被改写，复现就失去意义。

JSON 列（`input` / `output` / `attributes` / `summary`）存结构化内容；SQLite 的 JSON 支持足够本地 MVP 使用，
但要注意：**这些列不可被 SQL 直接检索**，需要按字段查询时得先做列提取。

---

## 8. 扩展点

**接入一个新的框架**

回放核心（`replay/engine.py`、`replay/effects.py`、`replay/fork.py`）不认识任何框架，只处理事件序列与策略解析。
录制可信度检查（事件边界、`seq` 缺口、脱敏、录制丢失、上下文截断、初始状态未被恢复）与
回放上下文的判定（`ensure_replay_context`）都在这层，因此不依赖框架也能被单元测试覆盖；
适配层只负责据此驱动一次执行。
新框架需要实现的是一个适配层，职责只有两件：拦截每一步、按计划返回录制结果或真实执行。
LangGraph 的实现见 `replay/langgraph_adapter.py`，可作为参照。

**接入一个新的 Agent**

通过 entry point 组 `afr.agents` 注册，平台据此发现"怎么重建这个 Agent"：

```toml
[project.entry-points."afr.agents"]
my-agent = "my_package.agent:agent_spec"
```

未注册的 Agent 依然可以正常录制与查看，只是服务端无法替你发起回放。

**新增断言类型**

在 `server/assertions.py` 的 `SUPPORTED_TYPES` 与求值分支里增加，并在描述函数里补上人话。
前端的标签映射在 `web/src/utils/format.ts`。新增类型后应当补一条测试。

**新增语言 SDK**

协议已在 `docs/protocol.md` 冻结：实现一个能发 `POST /v1/ingest` 的客户端即可，服务端不需要改动。

---

## 9. 测试策略

分三层，各管一件事：

| 层 | 位置 | 覆盖什么 |
| --- | --- | --- |
| SDK 单元 | `sdk/tests/` | 协议模型与策略解析、脱敏规则、序列化健壮性、录制链路的不抛异常保证、回放引擎的框架无关部分 |
| 服务端 | `server/tests/` | 入库与去重、脱敏、API 契约、差异计算、断言求值、用例执行 |
| 端到端 | `examples/langgraph_sre_agent/tests/` | 真实 LangGraph Agent 跑完"录制 -> 复现 -> 回归 -> 对比 -> 用例"整条链路 |

**最重要的一条是复现模式的确定性测试。** 它断言除时间戳、ID 与 `effect_source` 外，
回放与父 Run 的行为内容逐字段一致。这条过不了，后面所有能力都不成立。

端到端测试刻意用真实的 LangGraph Agent 与真实的 mock 工具，而不是打桩的假流程，
因为回放要验证的恰恰是框架交互这一层。

---

## 10. 已知限制与技术债

| 项 | 说明 | 影响 |
| --- | --- | --- |
| 只有一条框架适配路径经过验证 | 回放核心号称框架无关，但LangGraph 与 pi SDK 有离线契约测试，真实模型结果另见验证记录 | 第二个框架接入时可能发现抽象漏了点东西 |
| 回放中间状态全在内存 | 超大运行的回放可能吃紧 | 长任务场景需要评估落盘 |
| 预算只到「单批」这一层 | 单次回放与整批都可声明上限（整批按剩余额度逐格执行、触顶的格子如实标注未启动），但没有跨批次的预算池 / 配额 | 跨团队或长期运行的配额分配需要另做，不是当前能力 |
| Diff 未缓存 | 每次请求重新计算 | 运行很大时响应变慢 |
| 前端对齐可读性 | 两条运行步骤数差异很大时，对齐结果不易读 | 影响体验，不影响正确性 |
| 单用户、无鉴权 | 只监听 localhost，无多租户字段 | 生产部署前必须补，属于已知的范围边界而非疏漏 |
| 脱敏依赖规则表 | 规则之外的自定义凭证形态不会被识别 | 需要按企业实际形态扩展规则 |

---

## 11. 目录地图

```
sdk/agent_flight_recorder/
  models.py              协议数据模型与 EffectPolicy 解析（服务端复用）
  recorder.py            录制器、缓冲、上报、doctor 自检
  middleware.py          LangChain 接入层（拦截模型与工具调用）
  registry.py            Agent 注册表（entry point 发现）
  side_effects.py        工具副作用声明与解析
  redact.py              脱敏规则（SDK 与服务端共用）
  serialization.py       运行时对象 -> 可落库结构
  client.py              上报传输（标准库实现）
  cost.py                成本估算
  otel.py                OTel GenAI 语义约定导出
  replay/
    engine.py            框架无关核心：策略解析、步骤计划、会话状态
    effects.py           父 Run 的录制效果库（按名称与参数匹配）
    fork.py              行为步骤定义与分叉判定
    langgraph_adapter.py LangGraph 适配层与回放驱动

server/afr_server/
  main.py                FastAPI 应用与全部端点
  storage.py             入库、脱敏、查询
  tables.py              表结构
  replay_runner.py       回放调度
  cases.py               用例创建与执行
  assertions.py          确定性断言
  diff.py                差异计算
  seed.py                首次启动播种（运行 + Agent 自带用例）
  db.py / config.py      连接与配置
  transport.py           服务端内部直写通道
  schemas.py             API 请求响应模型

web/src/
  views/                 RunsView / RunDetailView / DiffView / CasesView
  components/            StepRow、各类徽标、状态面板、JSON 展示
  api/                   类型定义与 HTTP 客户端（含 SSE）
  styles/tokens.css      设计令牌

examples/langgraph_sre_agent/
  sre_agent/agent.py     示例 Agent 构建与平台注册
  sre_agent/tools.py     自带 mock 的 SRE 工具集
  sre_agent/scripted_model.py  离线确定性模型（两套剧本）
  sre_agent/prompts.py   默认版与修正版 Prompt
```
