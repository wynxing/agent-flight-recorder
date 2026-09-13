import type { EffectSource, EventType, RunStatus, SideEffect } from '@/api/types'

export function formatDuration(ms?: number | null): string {
  if (ms === null || ms === undefined) return '-'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1000)
  return `${minutes}m ${seconds}s`
}

export function formatNumber(value?: number | null): string {
  if (value === null || value === undefined) return '-'
  return value.toLocaleString('zh-CN')
}

export function formatCost(value?: number | null): string {
  if (!value) return '-'
  if (value < 0.01) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}

export function formatTime(iso?: string | null): string {
  if (!iso) return '-'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '-'
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function formatClock(iso?: string | null): string {
  if (!iso) return '-'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '-'
  return date.toLocaleTimeString('zh-CN', { hour12: false, fractionalSecondDigits: 3 })
}

export function relativeTime(iso?: string | null): string {
  if (!iso) return '-'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '-'
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 60) return `${seconds} 秒前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}

export function shortId(id?: string | null, length = 8): string {
  if (!id) return '-'
  return id.slice(0, length)
}

export function prettyJson(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

export function inlineJson(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

export function truncate(text: string, limit = 160): string {
  if (text.length <= limit) return text
  return `${text.slice(0, limit)}...`
}

export const EVENT_LABELS: Record<EventType, string> = {
  run_started: '开始',
  model_call: '模型调用',
  tool_call: '工具调用',
  state_snapshot: '状态快照',
  error: '错误',
  run_finished: '结束',
}

export const STATUS_LABELS: Record<RunStatus, string> = {
  running: '进行中',
  succeeded: '成功',
  failed: '失败',
  aborted: '已中止',
}

export const EFFECT_SOURCE_LABELS: Record<EffectSource, string> = {
  live: '真实执行',
  recorded: '录制回放',
  dry_run: '拦截未执行',
  blocked: '无法生成结果',
}

export const SIDE_EFFECT_LABELS: Record<SideEffect, string> = {
  read: '只读',
  write: '写入',
  external: '对外动作',
}

export const FORK_LABELS: Record<string, string> = {
  step_sequence: '步骤类型变化',
  tool_sequence: '工具序列变化',
  tool_args: '工具参数变化',
  model_output: '模型输出变化',
  error: '错误状态变化',
  length: '步骤数量变化',
}

export const ASSERTION_LABELS: Record<string, string> = {
  no_error: '没有错误',
  final_output_contains: '结论包含',
  final_output_not_contains: '结论不包含',
  final_output_matches: '结论匹配正则',
  tool_called: '调用过工具',
  tool_not_called: '未调用工具',
  tool_sequence_equals: '工具序列等于',
  max_tool_calls: '工具调用数上限',
}

// ------------------------------------------------------------------ 「无法判断」的成因
//
// 成因分类是跨层共享的闭集：取值与 SDK 的 InconclusiveCode（Python）、pi 的
// InconclusiveCode（TypeScript）逐字一致，对齐清单见 docs/replay-semantics.md 第 8 节。
// 服务端已经把 metadata.afr_replay / case.last_cause 归一化成 {code, detail}，
// 因此这里只消费结构化结果，不再自建第二份别名表。

export const INCONCLUSIVE_CODES = [
  'incomplete_recording',
  'event_sequence_gap',
  'redacted_replay_data',
  'recording_loss',
  'truncated_context',
  'missing_recorded_response',
  'missing_initial_state',
  'model_context_changed',
  'final_output_changed',
  'side_effect_blocked',
  'unknown',
] as const

export type InconclusiveCode = (typeof INCONCLUSIVE_CODES)[number]

export interface InconclusiveCause {
  code: InconclusiveCode
  detail: string
}

/**
 * 视觉分档。录制质量与「副作用被拦截」是两件完全不同的事，不能同色；
 * 集合之外落 unknown，用中性色表示「还说不清」。
 */
export type CauseTone = 'recording' | 'blocked' | 'unknown'

/** 每个成因的中文说明与**可操作的下一步**。只说「无法判断」而没有下一步是不合格的。 */
export const CAUSE_INFO: Record<InconclusiveCode, { label: string; action: string; tone: CauseTone }> = {
  incomplete_recording: {
    label: '录制缺少边界',
    action: '录制缺少开始或结束边界，无法确认它是不是完整的一次执行。建议重新录制这次运行。',
    tone: 'recording',
  },
  event_sequence_gap: {
    label: '事件序列有缺口',
    action: '事件的 seq 不连续，说明中间丢过事件，任何一步都可能对不上。建议重新录制这次运行。',
    tone: 'recording',
  },
  redacted_replay_data: {
    label: '录制含脱敏数据',
    action: '证据已被脱敏替换，不再逐字可比。请用未命中脱敏规则的录制重新跑一次。',
    tone: 'recording',
  },
  recording_loss: {
    label: '录制过程中丢事件',
    action: '录制方或服务端记录器丢过事件，证据不完整。建议重新录制这次运行。',
    tone: 'recording',
  },
  truncated_context: {
    label: '上下文被截断',
    action: '模型输入的消息没有完整保存（只保留了最近若干条）。请提高录制上限后重新录制。',
    tone: 'recording',
  },
  missing_recorded_response: {
    label: '缺少录制结果',
    action: '父 Run 里没有这一步的录制结果，回放已偏离原始轨迹。改用回归模式让工具真实执行，或检查 Agent 行为为何改变。',
    tone: 'recording',
  },
  missing_initial_state: {
    label: '初始状态未恢复',
    action: '父 Run 记录了初始 input，但回放既没有拿到 initial_state，也没有 task_only 声明。请提供状态或显式声明只跑 task。',
    tone: 'recording',
  },
  model_context_changed: {
    label: '模型上下文变化',
    action: '这次回放的模型输入与录制不一致，无法逐字复现。请确认模型或 Prompt 是否被改动。',
    tone: 'recording',
  },
  final_output_changed: {
    label: '结论与录制不同',
    action: '复现出来的结论与录制不一致，说明 Agent 行为已经改变。请看运行对比定位第一个分叉点。',
    tone: 'recording',
  },
  side_effect_blocked: {
    label: '副作用被拦截',
    action: '副作用被闸门拦截，本次执行没有真实发生——这不是结论不通过。确认安全后可显式允许真实执行再跑一次。',
    tone: 'blocked',
  },
  unknown: {
    label: '未知成因',
    action: '这个成因不在已知分类里，原始说明已保留在下方。请据此排查，并在上游补齐分类。',
    tone: 'unknown',
  },
}

/**
 * 把服务端归一化过的值读成成因。集合之外的取值显式落到「未知成因」并保留原文，
 * 因此历史数据或未知取值不会被当成某个已知成因。
 */
export function causeOf(value: unknown): InconclusiveCause | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const raw = typeof record.code === 'string' ? record.code : ''
  const detail = typeof record.detail === 'string' ? record.detail : ''
  if ((INCONCLUSIVE_CODES as readonly string[]).includes(raw)) {
    return { code: raw as InconclusiveCode, detail }
  }
  return { code: 'unknown', detail: detail || raw || JSON.stringify(value) }
}

export function causeInfo(code?: string | null) {
  const known = (INCONCLUSIVE_CODES as readonly string[]).includes(code ?? '')
  return CAUSE_INFO[known ? (code as InconclusiveCode) : 'unknown']
}

export function toolCallsOf(event: { output?: Record<string, unknown> | null }): { name?: string; args?: unknown }[] {
  const calls = event.output?.tool_calls
  if (!Array.isArray(calls)) return []
  return calls.filter((call): call is { name?: string; args?: unknown } => typeof call === 'object' && call !== null)
}

export function textOf(event: { output?: Record<string, unknown> | null }): string {
  const text = event.output?.text
  return typeof text === 'string' ? text : ''
}

export function argsOf(event: { input?: Record<string, unknown> | null }): unknown {
  return event.input?.args ?? null
}

