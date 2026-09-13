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

### 5.2 需要展开的几条

**D5 为什么错误事件不参与步骤对齐**

这是一个在实现过程中真实踩过的坑。最初把 `error` 也当作行为步骤，结果是：父 Run 里的一个错误事件占了位置，
但回放过程不会为 `error` 推进游标，后续步骤整体错位，"第一个不同的步骤"随之失去意义。
现在只有 `model_call` 与 `tool_call` 参与对齐；工具执行失败仍然会记录在 `tool_call` 的 `error` 字段里，信息不丢。

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
| SQLite | WAL 模式 | 让"边录制边读时间线"不会互相阻塞 |

**回放不占请求线程。** 提交回放后立即返回 `run_id`，前端通过 SSE 或轮询观察进度。

---

## 7. 存储设计

| 表 | 关键字段 | 约束 |
| --- | --- | --- |
| `runs` | `id`, `agent_name`, `status`, `parent_run_id`, `replay_from_seq`, `effect_policy`, `summary`, `event_count` | `parent_run_id` 建索引，便于查血缘 |
| `events` | `id`, `run_id`, `seq`, `type`, `name`, `input`, `output`, `error`, `tokens`, `side_effect`, `effect_source` | **唯一约束 `(run_id, seq)`**，这是幂等性的物理保证 |
| `cases` | `id`, `source_run_id`, `from_seq`, `assertions`, `effect_policy`, `last_status`, `last_results` | `source_run_id` 建索引 |

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
| Python 路径无统一回放预算上限 | 批量跑用例的 token 花费不受约束 | 财务风险，v1.1 必须补 |
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
