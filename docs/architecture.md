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
examples/langgraph_sre_agent      示例 Agent（LangGraph）+ 平台可发现的注册信息
examples/openai_agents_sre_agent 示例 Agent（OpenAI Agents SDK），同一份场景
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

第二个框架（OpenAI Agents SDK）接入后这条约束由 `replay/adapters.py` 继续守着：那张表存的是
**模块路径**而不是模块对象，因此 `import agent_flight_recorder` 不会把任何一个框架拖进来
（`sdk/tests/test_replay_adapters.py` 用子进程核对这一点）。

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
| `CaseSetVersion` | `server/.../case_versions.py` | 一次批量提交所用到的一组用例定义，按内容摘要寻址（见第 4.5 节） |
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

### 4.5 批量套件与用例集版本

```
POST /v1/suites
  -> 读出这一批用到的用例行，**在提交这一刻**取一次定义快照（每格一份）
     -> 对快照里「判据相关」的那一面算内容摘要 -> 用例集版本标识 cs1:<sha256>
        -> 固化成 case_set_versions 的一行（标识即主键，同内容天然复用同一行）
           -> 与套件行、格子行**同一个事务**写入（新套件不可能有标识却查不到定义）
              -> 每格在后台按**自己那份快照**执行，执行期不再读用例行

GET /v1/suites/{id}                    -> case_set: {id, case_count, recorded, drift}
GET /v1/case-set-versions/{version_id} -> 当时固化的定义（逐条用例的 source_run_id / 起点 /
                                          断言集 / 模式与模型覆盖）
PATCH /v1/cases/{id}                   -> 改定义（只改给出的字段），已落库的结论一个字不动
```

**为什么在提交那一刻冻结，而不是每次执行时读当前行。** 批次是并发跑的，用例行又是可变的。
如果每一格都读当前行，那么批次执行期间的一次编辑就会把一批结论悄悄劈成两个定义下的结果，
而汇总、版本号、结论计数仍然只有一个——即「前半批按旧断言、后半批按新断言，却报成一个数」。
冻结之后整批共享同一份定义，这个岔路在结构上就不存在（用例行被删掉也不影响：格子照样按冻结的
定义给出结论）。

**为什么版本只覆盖「判据相关」的那一面。** 计入的是 source_run_id、from_seq、to_seq、assertions
与 effect_policy（preset / policy / model / system_prompt）。不计入名字、描述、labels 与 `last_*`：
前三个改不出任何一条不同的结论（把改名算进去只会让每次改名都喊「用例变了」），而 `last_*` 每跑
一次就变——算进去的话，同一份定义跑第二遍就会得到一个新版本，版本化立刻失去意义。
规范化规则（字典键序无关、断言按 `AssertionSpec` 归一化、值里的空白有意义、断言列表顺序有意义、
用例提交顺序无关）逐条钉在 `server/tests/test_suite_versions.py` 里。

**归属与「用例变了」是两件事。** 归属永远在提交那一刻（版本标识 + 那版定义）；drift 只是如实提示
「用例页现在看到的内容已经不是这一版了」。控制台把这两句一起说，因为只说后者会让读者以为这一批的
结论也跟着变了。历史批次没有版本记录时读出来就是「无版本记录」，**不按当前用例反推一个版本号**。

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
| D15 | 批次的用例定义在**提交那一刻**冻结，每格按快照执行 | 执行期读可变行会让一批结论被编辑劈成两半 |
| D16 | 用例集版本 = 判据相关字段的内容摘要（`cs1:` 前缀即规范化方案号） | 标识相同必然定义相同，「是不是同一版」不必事后比对；换算法换前缀，旧标识仍可解释 |

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
| `suites` | `id`, `status`, `case_ids`, `conditions`, `budget`, `budget_usage`, `case_set_version` | 生命周期由格子的状态推导，落库值只是缓存 |
| `suite_items` | `id`, `suite_id`, `case_id`, `position`, `condition_key`, `condition`, `case_set_version`, `status` | **唯一约束 `(suite_id, case_id, condition_key)`**：一个套件里同一用例 × 同一条件只能有一格 |
| `case_set_versions` | `id`（`cs1:<sha256>`，主键）, `canonicalization`, `case_count`, `cases` | 主键即内容摘要：**同一份定义只可能有一行**，版本可去重、可反查 |

`cases.last_cause` 存最近一次结论的结构化成因（`{code, detail}`），契约为「结论不是 passed / failed 时非空」：
只有结论没有成因，用户无从知道该去看录制质量、副作用策略还是执行本身。新增可空列的补齐由 `db._add_missing_columns` 在启动时做（可重入，SQLite 单文件），
因此已有库不需要重建。

`case_set_versions` 只存**判据相关**的那一面（见第 4.5 节），因此「版本行的内容重算之后必须等于它的
标识」是一条可直接断言的不变量（`test_the_stored_version_content_recomputes_to_its_id`）。
版本化新增的三个可空列（`suites.case_set_version`、`suite_items.case_set_version`、
`cases.last_definition_digest`）走的是同一条补列路径；存量行补出来是 NULL，读出来就是
「无版本记录」与「最近一次结论没有定义记录」——**不回填**，因为回填等于替历史批次编一个它从来没有的
前提。新表由 `create_all` 在建库时创建，旧库直接补上即可（`server/tests/test_suite_versions.py`
用「把库退回旧形态再跑一次启动迁移」的方式钉住了这条路径）。

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
LangGraph 的实现见 `replay/langgraph_adapter.py`，第二个框架（OpenAI Agents SDK）见
`replay/openai_agents_adapter.py`：两者接管方式不同，正好可以互相参照。

**框架适配边界**（第二个框架接入时才补上的那一层）

在这之前「框架无关」只到核心为止：平台侧直接 import 了 LangGraph 适配层，Agent 注册表里
也没有任何字段说明「这个 Agent 该用哪个适配器重建」，于是「框架无关」在服务平台那一层
并不成立。现在：

* `AgentSpec.runtime` 由 Agent 包自己声明（默认 `langgraph`，因此既有 Agent 的回放路径不变）；
* `replay/adapters.py` 把 runtime 解析成适配器模块：未知取值是配置错误，依赖缺失报「缺哪个包」，
  两者都不回落到「猜一个框架」；
* `server/afr_server/replay_runner.py` 按声明选适配层，自己不再认识任何具体框架。

适配器模块的契约（两个实现都满足）：

```
run_replay(*, session, recorder, agent_factory, tool_side_effects, ...) -> ReplayResult
```

`agent_factory` 的调用约定由适配器自己定义（LangGraph 侧传 `middleware=`，Agents SDK 侧直接
替换 `Agent.model` 与 `Agent.tools`），因此 `AgentSpec.build` 不需要知道框架细节。

| 面向 | LangGraph 适配层 | OpenAI Agents SDK 适配层 |
| --- | --- | --- |
| 模型调用拦截 | `AgentMiddleware.wrap_model_call` | 实现 Model 协议（`get_response`） |
| 工具调用拦截 | `AgentMiddleware.wrap_tool_call` | 替换 `FunctionTool.on_invoke_tool` |
| 步边界状态 | `before_model(state)` 回调 | 模型请求的 `input` 条目（就是那一刻的状态） |
| 循环上限 | `config["recursion_limit"]` | `Runner.run(max_turns=...)` |
| 工具异常 | 冒到中间件上，可如实记录 | 默认被换成给模型看的文本，靠 SDK 留下的标记识别 |
| 失败收尾与成因 | 共用 `replay/boundary.py`：从异常链恢复成因 | 同左（Agents SDK 会把引擎异常包一层 `UserError`） |

两个适配层共用 `replay/boundary.py` 的成因恢复与失败收尾，因此「同一份录制在两个框架上回放、
结论一致」不是靠两边各写对一遍来保证的。工具失败另有一条框架差异：Agents SDK 默认把工具异常
换成一句给模型看的文本（Agent 行为不变），录制与回放靠 `sdk_converted_a_failure` 识别并记成失败；
**Agent 自己配置了 `failure_error_function` 时无法区分**（框架把失败当成了它自己的取值语义），
这一条如实留在未验证面里。

**这条边界验证到哪一层**（如实标注，不把「已验证的边界」写成「已证明」）：

* 已验证：录制 → 复现 → 回归 → 对比 → 用例判定的整条闭环在两个框架上都跑得通；同一份录制
  分别用两个适配层回放，**参与位置对齐的语义内容（行为指纹）与最终产出相等**；复现模式不产生
  任何真实调用；副作用闸门默认拦截、显式允许时真实执行并留警告；成因分类与预算触顶的语义一致。
  证据是两个 example 的离线端到端测试，以及 `sdk/tests/test_replay_adapters.py`、
  `sdk/tests/test_replay_openai_agents.py`。
* **不是**「除运行时字段外逐字段相等」：框架自己的消息与状态外壳（`input.messages`、
  `output.message`、`output.state`）在两个框架里本来就不一样（LangChain 是 `AIMessage` 的 dict，
  Agents SDK 是 Responses API 的条目），运行身份字段（`id` / `run_id` / `started_at`）每次回放也都是新的。
  跨框架的字段级边界由 `examples/openai_agents_sre_agent/tests/test_agents_scenario.py::
  test_cross_framework_difference_is_confined_to_the_framework_envelope` 逐字段钉住：差异只允许落在这两类字段上，
  `side_effect` / `error` / `tokens` / `attributes` 等语义字段一个都不许不同。
* 未验证：流式调用（`Runner.run_streamed` / `stream_response`——适配层直接报错，不做静默丢事件）、
  真实模型（本轮全程离线剧本模型，零成本）、handoffs / guardrails / MCP / 会话持久化等 Agents SDK
  特性、多 Agent 协作与并行工具调用、以及上面那条自定义 `failure_error_function` 的失败识别。
  接入第三个框架（自研 Runtime）时，这些面仍可能暴露新的抽象缺口。

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
| 端到端（第二框架） | `examples/openai_agents_sre_agent/tests/` | 同一套闭环在 OpenAI Agents SDK 上重跑，并做跨框架对比：两个框架录出的**行为指纹**（语义内容）一致；同一份录制用两个适配层回放，行为指纹与最终产出都相等，且差异只落在框架外壳与运行身份字段上 |

**最重要的一条是复现模式的确定性测试。** 它断言除时间戳、ID 与 `effect_source` 外，
回放与父 Run 的**语义内容（行为指纹）与最终产出**一致。这条过不了，后面所有能力都不成立。

端到端测试刻意用真实的 LangGraph Agent 与真实的 mock 工具，而不是打桩的假流程，
因为回放要验证的恰恰是框架交互这一层。

两个 example 的端到端测试都全程离线（脚本模型 + 关掉 tracing），不需要任何模型凭证：
「离线可跑」因此不是靠环境碰巧干净，而是进 `scripts/test.ps1` / CI 的一条真实步骤。

---

## 10. 已知限制与技术债

| 项 | 说明 | 影响 |
| --- | --- | --- |
| 框架适配层有两条路径经过验证，但不覆盖全部调用形态 | 回放核心的框架无关性已由第二个框架（OpenAI Agents SDK）的离线闭环支撑：两个框架录出的**行为指纹**（语义内容）一致；同一份录制用两个适配层回放，行为指纹与最终产出相等，差异只落在框架消息外壳与运行身份字段（第 8 节「这条边界验证到哪一层」）。**流式调用、真实模型、handoffs / guardrails / MCP、自定义 `failure_error_function` 的失败识别都还没验证过** | 未验证的那几面接入时仍可能暴露新的抽象缺口；这类缺口一旦出现，按本轮的处置方式修实现并补会真变红的测试 |
| 回放中间状态全在内存 | 超大运行的回放可能吃紧 | 长任务场景需要评估落盘 |
| 预算只到「单批」这一层 | 单次回放与整批都可声明上限（整批按剩余额度逐格执行、触顶的格子如实标注未启动），但没有跨批次的预算池 / 配额 | 跨团队或长期运行的配额分配需要另做，不是当前能力 |
| 用例集版本只解决「归属」 | 提交时固化定义、按内容摘要寻址、用例变更可见、批次中途被改不影响归属；**不做**跨版本的对比视图、版本迁移 / 合并 / 分支、可编辑的版本名。版本化之前创建的批次没有版本记录，读出来就是「无版本记录」（不回填） | 跨版本比较仍要人工看两份定义；历史批次的结论无法追溯归属 |
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
  openai_agents.py       OpenAI Agents SDK 接入层（Model 协议 + FunctionTool 包装）
  identifiers.py         运行时对象 -> 稳定标识（框架无关，两个接入层共用）
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
    boundary.py          边界层：成因恢复、失败收尾、Run 头写入（两个适配层共用）
    adapters.py          runtime -> 适配器模块的解析表（惰性导入，核心不拖进框架）
    langgraph_adapter.py LangGraph 适配层与回放驱动
    openai_agents_adapter.py  OpenAI Agents SDK 适配层与回放驱动

server/afr_server/
  main.py                FastAPI 应用与全部端点
  storage.py             入库、脱敏、查询
  tables.py              表结构
  replay_runner.py       回放调度
  cases.py               用例创建与执行
  case_versions.py       用例定义的规范化与用例集版本（内容决定的标识、固化、漂移）
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

examples/openai_agents_sre_agent/
  oa_sre_agent/agent.py   示例 Agent 构建与平台注册（runtime = openai-agents）
  oa_sre_agent/tools.py   同一批 SRE 工具的 Agents SDK 版（返回同一份数据）
  oa_sre_agent/scripted_model.py  实现 Model 协议的离线剧本模型
  oa_sre_agent/prompts.py 与 LangGraph 版共用同一份场景文本
```
