// 与 docs/protocol.md 对齐的类型。服务端直接复用同一套 pydantic 模型，
// 因此这里的字段名与线上载荷一一对应。

export type EventType =
  | 'run_started'
  | 'model_call'
  | 'tool_call'
  | 'state_snapshot'
  | 'error'
  | 'run_finished'

export type SideEffect = 'read' | 'write' | 'external'
export type EffectSource = 'live' | 'recorded' | 'dry_run' | 'blocked'
export type EffectMode = 'recorded' | 'live' | 'dry_run'
export type RunStatus = 'running' | 'succeeded' | 'failed' | 'aborted'
export type ReplayPreset = 'reproduce' | 'regress'
export type ToolCallState = 'same' | 'changed' | 'only_a' | 'only_b'

export interface TokenUsage {
  input?: number | null
  output?: number | null
  total?: number | null
}

export interface ErrorRecord {
  type: string
  message: string
  stack?: string | null
}

export interface AgentEvent {
  id: string
  run_id: string
  seq: number
  type: EventType
  parent_seq?: number | null
  source_seq?: number | null
  name?: string | null
  started_at: string
  ended_at?: string | null
  duration_ms?: number | null
  input?: Record<string, unknown> | null
  output?: Record<string, unknown> | null
  error?: ErrorRecord | null
  tokens?: TokenUsage | null
  cost_usd?: number | null
  side_effect?: SideEffect | null
  effect_source?: EffectSource | null
  redactions?: string[]
  attributes?: Record<string, unknown>
}

export interface EffectPolicy {
  default: EffectMode
  by_kind: Record<string, EffectMode>
  by_seq: Record<string, EffectMode>
  allow_side_effect_execution: boolean
}

export interface RunRecord {
  id: string
  agent_name: string
  agent_version?: string | null
  model?: string | null
  status: RunStatus
  parent_run_id?: string | null
  replay_from_seq?: number | null
  effect_policy?: EffectPolicy | null
  prompt_version?: string | null
  labels: Record<string, string>
  metadata: Record<string, unknown>
  started_at: string
  ended_at?: string | null
  redactions: string[]
}

export interface RunSummary {
  event_count: number
  model_calls: number
  tool_calls: number
  error_count: number
  tokens_input: number
  tokens_output: number
  tokens_total: number
  cost_usd: number
  cost_is_estimate: boolean
  duration_ms?: number | null
  effect_counts: Record<string, number>
}

export interface RunListItem {
  run: RunRecord
  summary: RunSummary
  event_count: number
  is_replay: boolean
}

export interface RunListResponse {
  runs: RunListItem[]
  total: number
}

/**
 * 一次回放的硬上限。两个维度都可以不声明——「没声明」不等于「上限为 0」，
 * 它是「这一维不参与判定」，因此不设上限的回放行为与没有这套能力时一致。
 */
export interface ReplayBudget {
  max_cost_usd?: number | null
  max_model_calls?: number | null
}

/**
 * 回放结束后的预算记账：已用 / 上限 / 是否触顶。
 * 只有声明过上限的回放才带它——没声明过上限的回放不会凭空多出一个「上限：无」。
 */
export interface ReplayBudgetUsage {
  max_cost_usd?: number | null
  max_model_calls?: number | null
  /** 已用：真实发生的模型调用次数。 */
  model_calls_used: number
  /** 已用成本（估算）。null = 未知（模型不在价格表内 / 没有 token 记录），不是 0。 */
  cost_used_usd?: number | null
  cost_is_estimate: boolean
  /** 是否触顶。 */
  exceeded: boolean
  /** 触顶的那一维；没触顶时为 null。 */
  stopped_by?: 'model_calls' | 'cost' | null
  /** 是否存在无法定价的真实调用（成本记成「未知」的原因）。 */
  cost_unknown: boolean
  detail: string
}

/** 提交前的预估。cost_usd 为 null 就是「无法预估」，不是 0。 */
export interface ReplayEstimateResponse {
  parent_run_id: string
  from_seq: number
  /** 预计真实执行的模型调用次数。 */
  model_calls: number
  cost_usd?: number | null
  cost_is_estimate: boolean
  detail: string
}

export interface ReplayMeta {
  complete?: boolean
  /** 结构化成因；服务端已把历史自由文本 reason 归一化到这里。 */
  cause?: InconclusiveCause | null
  /** 旧字段：历史数据里可能是自由字符串，也可能是与 cause 相同的结构。 */
  reason?: string | InconclusiveCause | null
  parent_run_id?: string
  from_seq?: number
  policy?: EffectPolicy
  first_fork?: Fork | null
  forks?: Fork[]
  verdict?: string
  /** 预算记账；只有声明了上限的回放才有。 */
  budget?: ReplayBudgetUsage | null
}

/**
 * 「无法判断」的结构化成因：稳定码 + 给人看的说明。
 * 取值集合与 SDK / pi 的 InconclusiveCode 一致（见 docs/replay-semantics.md 第 8 节）。
 */
export interface InconclusiveCause {
  code: string
  detail: string
}

export interface RunDetailResponse {
  run: RunRecord
  summary: RunSummary
  event_count: number
  parent?: RunRecord | null
  children: RunRecord[]
  replay?: ReplayMeta | null
  case?: { case_id?: string; name?: string } | null
  agent_registered: boolean
}

export interface TimelineResponse {
  run: RunRecord
  summary: RunSummary
  events: AgentEvent[]
}

export type ForkKind =
  | 'step_sequence'
  | 'tool_sequence'
  | 'tool_args'
  | 'model_output'
  | 'error'
  | 'length'

export interface Fork {
  kind: ForkKind
  message: string
  parent_seq?: number | null
  replay_seq?: number | null
  detail: Record<string, unknown>
}

export interface AlignedItem {
  index: number
  status: ToolCallState
  label: string
  a?: Record<string, unknown> | null
  b?: Record<string, unknown> | null
  detail: Record<string, unknown>
}

export interface SummaryDelta {
  field: string
  a?: number | null
  b?: number | null
  delta?: number | null
  hint?: string
}

export interface RunDiff {
  a_run_id: string
  b_run_id: string
  a_label: string
  b_label: string
  tools: AlignedItem[]
  models: AlignedItem[]
  summary: SummaryDelta[]
  final_output: { a?: string | null; b?: string | null; changed?: boolean }
  first_divergence?: Fork | null
  verdict: string
}

export interface AssertionSpec {
  type: string
  value?: unknown
  tool?: string | null
  args_contains?: Record<string, unknown> | null
  note?: string
}

export interface AssertionResult {
  spec: AssertionSpec
  passed: boolean
  detail: string
}

export interface CaseItem {
  id: string
  name: string
  description: string
  source_run_id: string
  from_seq?: number | null
  to_seq?: number | null
  assertions: AssertionSpec[]
  labels: Record<string, string>
  preset?: ReplayPreset | null
  policy?: EffectPolicy | null
  model?: string | null
  system_prompt?: string | null
  last_status?: 'running' | 'passed' | 'failed' | 'inconclusive' | 'error' | null
  last_run_id?: string | null
  last_run_at?: string | null
  last_results: AssertionResult[]
  /**
   * 最近一次结论的成因；结论为 passed / failed 时为 null。
   * 也就是说 inconclusive 与 error 都会带上它：error 的 code 说明为什么没有可信结论。
   */
  last_cause?: InconclusiveCause | null
  /** 最近一次执行用的条件；单条运行不携带条件时为 null。 */
  last_condition?: SuiteConditionInfo | null
  created_at?: string | null
  source_run?: RunRecord | null
}

/** 矩阵的一列：Prompt 版本与模型。null / 空表示沿用用例自身的设定。 */
export interface SuiteCondition {
  prompt?: string | null
  model?: string | null
}

/** 一个格子上实际用到的条件；system_prompt 是当时的 Prompt 正文，便于事后核对。 */
export interface SuiteConditionInfo {
  prompt?: string | null
  model?: string | null
  system_prompt?: string | null
  preset?: string | null
}

/**
 * 一个格子的状态闭集。前三个是未完成态（没有结论），后四个是有结论态。
 *
 * not_started 是「未启动（因批次预算用尽）」：格子从未被执行过。它与 pending 不是一回事
 * ——pending 是「还没轮到」，not_started 是「不会再轮到」。因此它既不是 failed，也不是
 * 「跑过了但拿不到结论」。
 */
export type SuiteItemStatus =
  | 'pending'
  | 'running'
  | 'not_started'
  | 'passed'
  | 'failed'
  | 'inconclusive'
  | 'error'

/**
 * 整批的预算记账。字段与单次回放的记账完全一样（服务端复用同一个 BudgetUsage），
 * 只多 not_started 一个计数——「有多少个格子根本没跑」是批次才有的概念。
 */
export interface SuiteBudgetUsage extends ReplayBudgetUsage {
  /** 因批次预算用尽而没跑的格子数：它们没有任何结论，不计入 failed 或 undecided。 */
  not_started: number
}

/** 套件里的一格：某条用例在某个条件下的结论。条件跟着结果一起回来，结论因此没有歧义。 */
export interface SuiteItem {
  id: string
  case_id: string
  case_name: string
  condition_key: string
  condition: SuiteConditionInfo
  status: SuiteItemStatus
  run_id?: string | null
  results: AssertionResult[]
  cause?: InconclusiveCause | null
  started_at?: string | null
  ended_at?: string | null
}

/**
 * 一个条件的汇总。
 *
 * 三个桶互斥且穷尽：total === determinable + undecided + unfinished。
 * undecided 只包括**跑过了、但拿不到可信结论**的格子（inconclusive + error）；
 * unfinished 是**还没跑完**的格子（pending + running），它没有任何结论，因此不属于
 * undecided。可判断率的分母永远是该条件自己的 total —— 服务端刻意不给出任何跨条件的
 * 合计分数（见 PRD 6.5），界面也不许自己算一个。
 */
export interface SuiteConditionGroup {
  condition_key: string
  condition: SuiteConditionInfo
  label: string
  total: number
  completed: number
  counts: Record<string, number>
  determinable: number
  undecided: number
  /**
   * 还没跑完的格子数（pending + running + not_started）。它不是结论，必须能与
   * undecided 区分开。
   */
  unfinished: number
  /** 其中「未启动（因批次预算用尽）」的格子数。它是 unfinished 的子集。 */
  not_started: number
  determinable_rate?: number | null
  errors: number
  items: SuiteItem[]
}

export interface SuiteDetail {
  id: string
  status: 'running' | 'finished'
  created_at?: string | null
  finished_at?: string | null
  case_ids: string[]
  conditions: SuiteCondition[]
  total: number
  completed: number
  counts: Record<string, number>
  errors: number
  /** 整批的预算记账；没声明过整批上限时为 null（不设上限不等于「上限为 0」）。 */
  budget?: SuiteBudgetUsage | null
  groups: SuiteConditionGroup[]
}

/** 整批的预估。cost_usd 为 null 就是「无法预估」：只要有一格给不出成本，整批就不报数字。 */
export interface SuiteEstimateResponse {
  cells: number
  model_calls: number
  cost_usd?: number | null
  cost_is_estimate: boolean
  detail: string
}

export interface SuiteSummary {
  id: string
  status: 'running' | 'finished'
  created_at?: string | null
  finished_at?: string | null
  conditions: SuiteCondition[]
  total: number
  completed: number
  counts: Record<string, number>
  errors: number
}

export interface SuiteListResponse {
  suites: SuiteSummary[]
}

export interface SuiteSubmitResponse {
  suite_id: string
  status: string
  total: number
  conditions: SuiteCondition[]
}

export interface AgentInfo {
  name: string
  description: string
  version: string
  default_model?: string | null
  default_system_prompt?: string | null
  prompt_presets: Record<string, string>
  tools: string[]
  can_replay: boolean
  can_seed: boolean
}

export interface ReplayResponse {
  run_id: string
  parent_run_id: string
  from_seq: number
  plan_summary: string
  status: string
}

export interface CaseRunResponse {
  case_id: string
  run_id: string
  status: string
}
