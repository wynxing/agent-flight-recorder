import type {
  AgentEvent,
  AgentInfo,
  CaseItem,
  CaseRunResponse,
  EffectMode,
  EffectPolicy,
  ReplayBudget,
  ReplayEstimateResponse,
  ReplayPreset,
  ReplayResponse,
  RunDetailResponse,
  RunDiff,
  RunListResponse,
  SuiteCondition,
  SuiteDetail,
  SuiteListResponse,
  SuiteSubmitResponse,
  TimelineResponse,
} from './types'

// 开发时由 Vite 代理到本地服务端，构建后由服务端同源托管，因此这里留空。
const BASE = ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch (error) {
    throw new ApiError(0, `无法连接本地服务端，请确认 afr-server 正在运行。`)
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const payload = await response.json()
      if (payload?.detail) detail = typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail)
    } catch {
      // 响应体不是 JSON，保留状态码文本
    }
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as T
}

export interface ReplayRequestPayload {
  from_seq: number
  preset?: ReplayPreset
  policy?: EffectPolicy
  /** 硬上限；不传就是「不设上限」，与没有这套能力时行为一致。 */
  budget?: ReplayBudget | null
  model?: string
  system_prompt?: string
  labels?: Record<string, string>
}

export interface CaseCreatePayload {
  name: string
  description?: string
  source_run_id: string
  from_seq?: number
  assertions: { type: string; value?: unknown; tool?: string; args_contains?: Record<string, unknown> }[]
  labels?: Record<string, string>
  preset?: ReplayPreset
  model?: string
  system_prompt?: string
}

export const api = {
  health: () => request<{ ok: boolean; db: string; version: string }>('/v1/health'),

  agents: () => request<AgentInfo[]>('/v1/agents'),

  runs: (params: { limit?: number; agent_name?: string; status?: string; roots_only?: boolean } = {}) => {
    const query = new URLSearchParams()
    if (params.limit) query.set('limit', String(params.limit))
    if (params.agent_name) query.set('agent_name', params.agent_name)
    if (params.status) query.set('status', params.status)
    if (params.roots_only) query.set('roots_only', 'true')
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<RunListResponse>(`/v1/runs${suffix}`)
  },

  run: (runId: string) => request<RunDetailResponse>(`/v1/runs/${runId}`),

  timeline: (runId: string) => request<TimelineResponse>(`/v1/runs/${runId}/timeline`),

  replay: (runId: string, payload: ReplayRequestPayload) =>
    request<ReplayResponse>(`/v1/runs/${runId}/replay`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 预估这次回放要花多少：不调用模型，也不创建 Run。 */
  estimateReplay: (runId: string, payload: ReplayRequestPayload) =>
    request<ReplayEstimateResponse>(`/v1/runs/${runId}/replay/estimate`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  diff: (a: string, b: string) => request<RunDiff>(`/v1/diff?a=${a}&b=${b}`),

  cases: () => request<{ cases: CaseItem[] }>('/v1/cases'),

  case: (caseId: string) => request<CaseItem>(`/v1/cases/${caseId}`),

  createCase: (payload: CaseCreatePayload) =>
    request<CaseItem>('/v1/cases', { method: 'POST', body: JSON.stringify(payload) }),

  runCase: (
    caseId: string,
    payload: {
      from_seq?: number;
      preset?: ReplayPreset;
      /** 这一次执行的硬上限；不传就是「不设上限」。 */
      budget?: ReplayBudget | null;
      model?: string;
      system_prompt?: string;
    },
  ) =>
    request<CaseRunResponse>(`/v1/cases/${caseId}/run`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  suites: (limit = 20) => request<SuiteListResponse>(`/v1/suites?limit=${limit}`),

  suite: (suiteId: string) => request<SuiteDetail>(`/v1/suites/${suiteId}`),

  /**
   * 发起一次批量运行：一组用例 × 一组条件。
   * 立刻返回批次标识，执行在后台；进度来自对批次查询的轮询。
   */
  runSuite: (payload: { case_ids?: string[]; all_cases?: boolean; conditions?: SuiteCondition[] }) =>
    request<SuiteSubmitResponse>('/v1/suites', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
}

export interface StreamHandlers {
  onStep: (event: AgentEvent) => void
  onDone: (status: string) => void
  onError: () => void
}

/** 尾随一个正在运行的 Run。返回值用于主动断开。 */
export function streamRun(runId: string, afterSeq: number, handlers: StreamHandlers): () => void {
  const source = new EventSource(`${BASE}/v1/runs/${runId}/events/stream?after_seq=${afterSeq}`)
  let closed = false

  source.addEventListener('step', (message) => {
    try {
      handlers.onStep(JSON.parse((message as MessageEvent).data) as AgentEvent)
    } catch {
      // 单条事件解析失败不应该中断整个尾随
    }
  })
  source.addEventListener('done', (message) => {
    const status = safeStatus((message as MessageEvent).data)
    close()
    handlers.onDone(status)
  })
  source.addEventListener('timeout', () => {
    close()
    handlers.onDone('timeout')
  })
  source.onerror = () => {
    if (closed) return
    close()
    handlers.onError()
  }

  function close() {
    closed = true
    source.close()
  }

  return close
}

function safeStatus(raw: string): string {
  try {
    return (JSON.parse(raw) as { status?: string }).status ?? 'unknown'
  } catch {
    return 'unknown'
  }
}

export type { EffectMode }
