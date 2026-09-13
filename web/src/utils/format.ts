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

