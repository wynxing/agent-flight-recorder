<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  PhCheckCircle,
  PhClock,
  PhPlay,
  PhPlus,
  PhProhibit,
  PhQuestion,
  PhTrash,
  PhXCircle,
} from '@phosphor-icons/vue'
import { api } from '@/api/client'
import type {
  CaseItem,
  ReplayBudget,
  SuiteBudgetUsage,
  SuiteCondition,
  SuiteConditionGroup,
  SuiteConditionInfo,
  SuiteDetail,
  SuiteEstimateResponse,
  SuiteItem,
  SuiteItemStatus,
} from '@/api/types'
import CausePanel from '@/components/CausePanel.vue'
import StatePanel from '@/components/StatePanel.vue'
import { useSessionStore } from '@/stores/session'
import { ASSERTION_LABELS, causeInfo, causeOf, relativeTime, shortId, truncate } from '@/utils/format'
import {
  causeDrillLabel,
  conditionLabel,
  conditionVerdict,
  determinableRateText,
  notStartedLabel,
} from '@/utils/suite'

const route = useRoute()
const session = useSessionStore()

const cases = ref<CaseItem[]>([])
const loading = ref(true)
const error = ref('')
const busyCase = ref('')
const promptChoice = ref<Record<string, string>>({})

// 批量运行：勾选用例、选条件、发起、看进度、看按条件分组的汇总。
const selected = ref<Record<string, boolean>>({})
const conditions = ref<SuiteCondition[]>([{ prompt: '', model: '' }])
const suite = ref<SuiteDetail | null>(null)
const suiteError = ref('')
const submitting = ref(false)
// 整批的预算上限（留空 = 不设上限，不是「上限为 0」）与提交前的整批预估。
const suiteBudget = ref({ cost: '', calls: '' })
const estimating = ref(false)
const estimate = ref<SuiteEstimateResponse | null>(null)

let poller: number | null = null
let suitePoller: number | null = null

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await api.cases()
    cases.value = response.cases
    if (response.cases.some((item) => item.last_status === 'running')) startPolling()
    await loadLatestSuite()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

async function loadLatestSuite() {
  try {
    const { suites } = await api.suites(1)
    if (suites.length) await loadSuite(suites[0].id)
  } catch {
    // 批次列表读不到不该影响用例列表：用例页的主职责仍然是逐条用例。
  }
}

async function loadSuite(suiteId: string) {
  suite.value = await api.suite(suiteId)
  if (suite.value.status === 'running') startSuitePolling()
  else stopSuitePolling()
}

async function refreshQuietly() {
  try {
    const response = await api.cases()
    cases.value = response.cases
    if (!response.cases.some((item) => item.last_status === 'running')) stopPolling()
  } catch {
    stopPolling()
  }
}

function startPolling() {
  if (poller !== null) return
  poller = window.setInterval(refreshQuietly, 1200)
}

function stopPolling() {
  if (poller === null) return
  window.clearInterval(poller)
  poller = null
}

async function refreshSuite() {
  if (!suite.value) {
    stopSuitePolling()
    return
  }
  try {
    const detail = await api.suite(suite.value.id)
    suite.value = detail
    if (detail.status !== 'running') {
      stopSuitePolling()
      await refreshQuietly()
    }
  } catch {
    stopSuitePolling()
  }
}

function startSuitePolling() {
  if (suitePoller !== null) return
  suitePoller = window.setInterval(refreshSuite, 900)
}

function stopSuitePolling() {
  if (suitePoller === null) return
  window.clearInterval(suitePoller)
  suitePoller = null
}

async function run(caseId: string) {
  busyCase.value = caseId
  const invalid = budgetErrorFor(caseId)
  if (invalid) {
    // 上限填得不对就不提交：与其跑出一个没人能解释的花费，不如当场说清楚。
    error.value = invalid
    busyCase.value = ''
    return
  }
  try {
    const presetName = promptChoice.value[caseId] ?? ''
    const item = cases.value.find((entry) => entry.id === caseId)
    const presets = presetsFor(item)
    const systemPrompt = presetName ? presets[presetName] : undefined
    // 选了 Prompt 版本就意味着要真的跑模型，因此固定按回归模式执行。
    await api.runCase(caseId, {
      system_prompt: systemPrompt,
      preset: systemPrompt ? 'regress' : undefined,
      budget: budgetForCase(caseId),
    })
    startPolling()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    busyCase.value = ''
  }
}

// ------------------------------------------------------------------ 选择与条件

const selectedIds = computed(() =>
  cases.value.filter((item) => selected.value[item.id]).map((item) => item.id),
)

const allSelected = computed(
  () => cases.value.length > 0 && selectedIds.value.length === cases.value.length,
)

function toggleAll() {
  if (allSelected.value) {
    selected.value = {}
    return
  }
  const next: Record<string, boolean> = {}
  for (const item of cases.value) next[item.id] = true
  selected.value = next
}

const promptOptions = computed(() => {
  const names = new Set<string>()
  for (const agent of session.agents) {
    for (const name of Object.keys(agent.prompt_presets)) names.add(name)
  }
  return [...names].sort()
})

const modelOptions = computed(() => {
  const names = new Set<string>()
  for (const agent of session.agents) if (agent.default_model) names.add(agent.default_model)
  for (const item of cases.value) {
    if (item.model) names.add(item.model)
    if (item.source_run?.model) names.add(item.source_run.model)
  }
  return [...names].sort()
})

function addCondition() {
  conditions.value.push({ prompt: '', model: '' })
}

function removeCondition(index: number) {
  conditions.value.splice(index, 1)
  if (!conditions.value.length) conditions.value.push({ prompt: '', model: '' })
}

function chosenConditions(): SuiteCondition[] {
  const rows = conditions.value.map((row) => ({
    prompt: row.prompt ? row.prompt : null,
    model: row.model ? row.model : null,
  }))
  return rows.length ? rows : [{ prompt: null, model: null }]
}

async function startSuite() {
  suiteError.value = ''
  const ids = selectedIds.value
  if (!ids.length) {
    suiteError.value = '先勾选要跑的用例，或点「全选」。'
    return
  }
  const problem = suiteBudgetIssue()
  if (problem) {
    suiteError.value = problem
    return
  }
  submitting.value = true
  try {
    const budget = parseBudget(suiteBudget.value)
    // 全选时走「全部用例」这条路，让服务端的这个入口也真的被用到。
    const response = allSelected.value
      ? await api.runSuite({ all_cases: true, conditions: chosenConditions(), budget })
      : await api.runSuite({ case_ids: ids, conditions: chosenConditions(), budget })
    await loadSuite(response.suite_id)
    startPolling()
  } catch (cause) {
    suiteError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    submitting.value = false
  }
}

/** 提交前先预估整批：不调用模型、也不创建批次。 */
async function estimateBatch() {
  suiteError.value = ''
  estimate.value = null
  const ids = selectedIds.value
  if (!ids.length) {
    suiteError.value = '先勾选要跑的用例，或点「全选」。'
    return
  }
  const problem = suiteBudgetIssue()
  if (problem) {
    suiteError.value = problem
    return
  }
  estimating.value = true
  try {
    const budget = parseBudget(suiteBudget.value)
    estimate.value = await api.estimateSuite({
      ...(allSelected.value ? { all_cases: true } : { case_ids: ids }),
      conditions: chosenConditions(),
      budget,
    })
  } catch (cause) {
    suiteError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    estimating.value = false
  }
}

// ------------------------------------------------------------------ 呈现

function presetsFor(item?: CaseItem) {
  if (!item?.source_run) return {}
  const agent = session.agents.find((entry) => entry.name === item.source_run?.agent_name)
  return agent?.prompt_presets ?? {}
}

function describeAssertion(assertion: { type: string; tool?: string | null; value?: unknown }) {
  const label = ASSERTION_LABELS[assertion.type] ?? assertion.type
  const target =
    assertion.tool ??
    (assertion.value !== undefined && assertion.value !== null ? String(assertion.value) : '')
  return target ? label + ': ' + target : label
}

function isHighlighted(caseId: string) {
  return route.query.highlight === caseId
}

// 「拿不到结论」与「结论是不通过」是两件事，不能共用一个叉。
// 前者用问号 + 琥珀色，后者才是表示失败的叉。
const STATUS_ICONS: Record<SuiteItemStatus, unknown> = {
  pending: PhClock,
  running: PhPlay,
  // 「未启动」既不通过也不失败：它是一个从未发生过的格子，因此用一个「未开始」的记号，
  // 而不是任何表示结论的图标。
  not_started: PhProhibit,
  passed: PhCheckCircle,
  inconclusive: PhQuestion,
  failed: PhXCircle,
  error: PhXCircle,
}

function statusIcon(status: string) {
  return STATUS_ICONS[status as SuiteItemStatus] ?? PhXCircle
}

const STATUS_TEXTS: Record<SuiteItemStatus, string> = {
  pending: '排队中',
  running: '正在执行',
  // 状态名只说「未启动」，原因由 notStartedLabel() 补齐（整批预算用尽）。
  not_started: '未启动',
  passed: '通过',
  inconclusive: '无法判断',
  failed: '未通过',
  error: '执行出错',
}

function statusText(status?: string | null) {
  if (!status) return ''
  return STATUS_TEXTS[status as SuiteItemStatus] ?? status
}

// ------------------------------------------------------------------ 预算上限
//
// 单次执行与整批共用同一套语义（也与运行详情页一致）：留空 = 不设上限（不是「上限为 0」）；
// 上限在 SDK 层生效，触顶时在步边界停下，已用 / 上限 / 是否触顶如实记账。
const budgetFields = ref<Record<string, { cost: string; calls: string }>>({})

function budgetInput(caseId: string) {
  return budgetFields.value[caseId] ?? { cost: '', calls: '' }
}

function setBudgetField(caseId: string, field: 'cost' | 'calls', value: string) {
  budgetFields.value = {
    ...budgetFields.value,
    [caseId]: { ...budgetInput(caseId), [field]: value },
  }
}

function budgetForCase(caseId: string): ReplayBudget | null {
  return parseBudget(budgetInput(caseId))
}

/** 把两个输入框读成一个上限对象；两个都空就是「不设上限」（null）。 */
function parseBudget(input: { cost: string; calls: string }): ReplayBudget | null {
  const budget: ReplayBudget = {}
  if (input.cost.trim()) budget.max_cost_usd = Number(input.cost.trim())
  if (input.calls.trim()) budget.max_model_calls = Number(input.calls.trim())
  return Object.keys(budget).length ? budget : null
}

function budgetErrorFor(caseId: string): string {
  return budgetIssue(budgetInput(caseId), '')
}

/** 上限填得不对就不提交：与其跑出一个没人能解释的花费，不如当场说清楚。 */
function budgetIssue(input: { cost: string; calls: string }, scope: string): string {
  for (const [label, raw] of [
    ['最大成本', input.cost],
    ['最大模型调用次数', input.calls],
  ] as [string, string][]) {
    const text = raw.trim()
    if (!text) continue
    const value = Number(text)
    if (!Number.isFinite(value) || value < 0) {
      return scope + label + '必须是不小于 0 的数字；留空表示不设上限。'
    }
  }
  return ''
}

function suiteBudgetIssue(): string {
  return budgetIssue(suiteBudget.value, '整批')
}

/** 已用成本：null 是「未知」，不是 0（模型不在价格表内时这一维无法判定）。 */
function costText(value?: number | null): string {
  if (value === null || value === undefined) return '未知'
  return '$' + value.toFixed(6)
}

/** 整批的上限写法；两个维度都可能各自没有声明。 */
function batchLimitText(budget: SuiteBudgetUsage): string {
  const parts: string[] = []
  if (budget.max_model_calls !== null && budget.max_model_calls !== undefined) {
    parts.push(budget.max_model_calls + ' 次真实模型调用')
  }
  if (budget.max_cost_usd !== null && budget.max_cost_usd !== undefined) {
    parts.push(costText(budget.max_cost_usd) + ' 成本')
  }
  return parts.length ? parts.join(' · ') : '未设置'
}

/** 触顶的是哪一维：与运行详情页用同一套说法。 */
function batchStopText(budget: SuiteBudgetUsage): string {
  if (budget.stopped_by === 'model_calls') return '模型调用次数'
  if (budget.stopped_by === 'cost') return '成本'
  return '预算'
}

function causeOfCase(item: CaseItem) {
  return item.last_status === 'inconclusive' ? causeOf(item.last_cause) : null
}

// 条件的标签只有一套规则（utils/suite.ts），服务端 suites.condition_label() 与它逐字一致，
// 由跨语言契约测试钉住：控制台不再自己造一份「默认模型」之类的说法。
function conditionText(condition?: SuiteConditionInfo | SuiteCondition | null) {
  return conditionLabel(condition ?? {})
}

function countOf(group: SuiteConditionGroup, status: SuiteItemStatus) {
  return group.counts[status] ?? 0
}

// 「还没跑」与「跑到一半拿不到结论」是两件事：判定与文案都在 utils/suite.ts 里，
// 由跨语言契约测试钉住（server/tests/test_suites.py）。
function verdictText(group: SuiteConditionGroup) {
  return conditionVerdict(group)
}

function rateText(group: SuiteConditionGroup) {
  return determinableRateText(group)
}

// 每一条的诊断信息：有成因就给成因，否则给第一条没过的断言。
// 无论哪一类，都不会把「拿不到结论」写成一个「未通过」。
function diagnosis(item: SuiteItem) {
  if (item.cause) return causeInfo(item.cause.code).label + '：' + item.cause.detail
  if (item.status === 'passed') return '断言全部通过'
  // 未启动的格子没有成因（成因只属于跑过了的那两类），它的原因由状态本身说清。
  if (item.status === 'not_started') return notStartedLabel()
  const failed = item.results.find((result) => !result.passed)
  if (failed) return failed.detail
  return item.status === 'pending' ? '等待执行' : '正在执行'
}

function withCause(group: SuiteConditionGroup) {
  return group.items.filter((item) => item.cause !== null && item.cause !== undefined)
}

onMounted(async () => {
  await session.loadAgents()
  await load()
})
onUnmounted(() => {
  stopPolling()
  stopSuitePolling()
})
</script>

<template>
  <section class="head">
    <div>
      <h1>回归用例</h1>
      <p class="lede">
        生产环境里的每一次失败都可以固定成一条用例。重跑用例会按设定回放原始运行，并用确定性断言给出通过或失败。
      </p>
    </div>
  </section>

  <StatePanel v-if="loading" variant="loading" />
  <StatePanel v-else-if="error" variant="error" title="无法读取用例" :detail="error" @retry="load" />
  <StatePanel
    v-else-if="!cases.length"
    variant="empty"
    title="还没有用例"
    detail="打开任意一次运行，在右侧面板里点「创建用例」，就能把这次失败沉淀成可重复验证的回归用例。"
  />

  <template v-else>
    <section class="batch">
      <header>
        <h2>批量运行</h2>
        <p class="hint">
          一次跑一批用例。汇总按条件分组呈现，不合成总分：每个条件的四态计数与可判断率各自成立，分母是该条件自己的用例数。
        </p>
      </header>

      <div class="picker">
        <div class="selection">
          <span class="block-label">用例</span>
          <button class="ghost" type="button" @click="toggleAll">
            {{ allSelected ? '清空选择' : '全选' }}
          </button>
          <span class="count">已选 {{ selectedIds.length }} / {{ cases.length }} 条</span>
        </div>

        <div class="conditions">
          <span class="block-label">条件（Prompt 版本 / 模型）</span>
          <div v-for="(row, index) in conditions" :key="index" class="condition-row">
            <select v-model="row.prompt" aria-label="Prompt 版本">
              <option value="">沿用用例自身</option>
              <option v-for="name in promptOptions" :key="name" :value="name">{{ name }}</option>
            </select>
            <input
              v-model="row.model"
              list="afr-model-options"
              placeholder="模型（沿用用例自身）"
              aria-label="模型"
            />
            <button
              class="ghost icon"
              type="button"
              :disabled="conditions.length === 1"
              title="移除这个条件"
              @click="removeCondition(index)"
            >
              <PhTrash :size="13" weight="bold" />
            </button>
          </div>
          <datalist id="afr-model-options">
            <option v-for="name in modelOptions" :key="name" :value="name" />
          </datalist>
          <button class="ghost add" type="button" @click="addCondition">
            <PhPlus :size="13" weight="bold" />
            再加一个条件
          </button>
        </div>
      </div>

      <!--
        整批的预算上限与单次执行是同一套语义：留空 = 不设上限（不是「上限为 0」）。
        声明之后整批按顺序逐格执行——只有逐格，「整批不超过上限」才是可保证的结论。
      -->
      <div class="budget-field">
        <span class="budget-label">整批预算上限（可选，留空表示不设上限）</span>
        <div class="budget-inputs">
          <label>
            <span>整批最大成本（USD）</span>
            <input
              v-model="suiteBudget.cost"
              type="text"
              inputmode="decimal"
              placeholder="不设上限"
            />
          </label>
          <label>
            <span>整批最大模型调用次数</span>
            <input
              v-model="suiteBudget.calls"
              type="text"
              inputmode="numeric"
              placeholder="不设上限"
            />
          </label>
        </div>
        <span class="budget-hint">
          到点后不再启动新格子；已启动的格子跑完并如实记账，没轮到的格子会被标成「未启动（因批次预算用尽）」。
        </span>
      </div>

      <p v-if="allSelected" class="hint">已全选，将按「全部用例」提交。</p>
      <p v-if="suiteError" class="suite-error">{{ suiteError }}</p>

      <div class="submit-row">
        <button class="primary" type="button" :disabled="submitting" @click="startSuite">
          <PhPlay :size="13" weight="bold" />
          {{ submitting ? '正在提交' : '发起批量运行' }}
        </button>
        <!-- 提交前先预估整批：不调用模型，也不创建批次；成本给不出来时如实说「无法预估」。 -->
        <button class="ghost" type="button" :disabled="estimating" @click="estimateBatch">
          {{ estimating ? '正在预估' : '先预估整批' }}
        </button>
      </div>
      <p v-if="estimate" class="estimate">
        预估：{{ estimate.cells }} 个格子 · 预计 {{ estimate.model_calls }} 次真实模型调用
        <br />{{ estimate.detail }}
      </p>
    </section>

    <section v-if="suite" class="suite">
      <header>
        <h2>
          批次 {{ shortId(suite.id) }}
          <span class="status" :class="suite.status === 'running' ? 'running' : 'passed'">
            <component :is="statusIcon(suite.status === 'running' ? 'running' : 'passed')" :size="13" weight="bold" />
            {{ suite.status === 'running' ? '进行中' : '已完成' }}
          </span>
        </h2>
        <p class="meta">
          <!-- 这里只有计数：格子总数、已完成数与出错条数。比率一律按条件分列，不做合计。 -->
          {{ suite.case_ids.length }} 条用例 × {{ suite.conditions.length }} 个条件 = {{ suite.total }} 个格子
          <span class="sep">/</span>已完成 {{ suite.completed }} / {{ suite.total }}
          <span class="sep">/</span><span :class="{ 'has-errors': suite.errors > 0 }">有 {{ suite.errors }} 条出错</span>
          <template v-if="suite.finished_at">
            <span class="sep">/</span>{{ relativeTime(suite.finished_at) }}结束
          </template>
        </p>
        <!-- 整批的预算记账：已用 / 上限 / 是否触顶。没声明过整批上限时整块不出现。 -->
        <p v-if="suite.budget" class="budget-line">
          整批预算上限 {{ batchLimitText(suite.budget) }}
          <span class="sep">/</span>已用 {{ suite.budget.model_calls_used }} 次真实模型调用
          <span class="sep">/</span>已用成本 {{ costText(suite.budget.cost_used_usd) }}
          <span class="sep">/</span>
          <span v-if="suite.budget.exceeded" class="budget-hit">
            已触顶（{{ batchStopText(suite.budget) }}）
          </span>
          <span v-else>未触顶</span>
        </p>
        <p v-if="suite.budget" class="budget-detail">{{ suite.budget.detail }}</p>
      </header>

      <ul class="groups">
        <li v-for="group in suite.groups" :key="group.condition_key">
          <header class="group-head">
            <!-- 汇总标签直接用服务端算好的 label：分组的口径只有一处。 -->
            <h3>{{ group.label }}</h3>
            <span class="progress">{{ group.completed }} / {{ group.total }} 已完成</span>
          </header>

          <!--
            进行中只说三个桶的计数；只有整批跑完才说「N 条中 M 条拿不到结论」。
            未跑完的格子从未执行过，不能对它下「拿不到」这个断言。
          -->
          <p class="verdict-line">
            {{ verdictText(group) }}
            <span class="sep">/</span>可判断率 {{ rateText(group) }}
          </p>

          <ul class="counts">
            <li class="passed">通过 {{ countOf(group, 'passed') }}</li>
            <li class="failed">未通过 {{ countOf(group, 'failed') }}</li>
            <li class="inconclusive">无法判断 {{ countOf(group, 'inconclusive') }}</li>
            <li class="error">执行出错 {{ countOf(group, 'error') }}</li>
            <!-- 未完成的格子单列一项，永远不并进「拿不到结论」：它没有结论，不是「拿不到」。 -->
            <li v-if="group.unfinished" class="pending">未完成 {{ group.unfinished }}</li>
            <!-- 「未启动」是未完成里的一个子集：它不是「还没轮到」，而是整批预算已经用尽。 -->
            <li v-if="group.not_started" class="not-started">
              {{ notStartedLabel() }} {{ group.not_started }}
            </li>
          </ul>

          <table class="items">
            <thead>
              <tr>
                <th>用例</th>
                <th>结论</th>
                <th>直接原因</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in group.items" :key="item.id">
                <td>
                  <RouterLink :to="{ name: 'cases', query: { highlight: item.case_id } }">
                    {{ item.case_name }}
                  </RouterLink>
                </td>
                <td>
                  <span class="status" :class="item.status">
                    <component :is="statusIcon(item.status)" :size="12" weight="bold" />
                    {{ statusText(item.status) }}
                  </span>
                </td>
                <td class="diagnosis">{{ truncate(diagnosis(item), 120) }}</td>
                <td class="links">
                  <RouterLink
                    v-if="item.run_id"
                    :to="{ name: 'run-detail', params: { id: item.run_id } }"
                  >
                    回放
                  </RouterLink>
                </td>
              </tr>
            </tbody>
          </table>

          <details v-if="withCause(group).length" class="drill">
            <summary>{{ causeDrillLabel(withCause(group).length) }}</summary>
            <div class="drill-body">
              <div v-for="item in withCause(group)" :key="item.id" class="drill-item">
                <p class="drill-title">
                  {{ item.case_name }}
                  <span class="status" :class="item.status">{{ statusText(item.status) }}</span>
                </p>
                <CausePanel :cause="causeOf(item.cause)" :title="statusText(item.status)" compact />
                <ul class="drill-results">
                  <li v-for="(result, index) in item.results" :key="index">
                    <span class="result" :class="{ pass: result.passed }">
                      {{ result.passed ? '通过' : '失败' }}
                    </span>
                    <span class="result-detail">{{ result.detail }}</span>
                  </li>
                </ul>
              </div>
            </div>
          </details>
        </li>
      </ul>
    </section>

    <ul class="cases">
      <li v-for="item in cases" :key="item.id" :class="{ highlighted: isHighlighted(item.id) }">
        <header>
          <div class="title-row">
            <input
              v-model="selected[item.id]"
              type="checkbox"
              class="pick"
              :aria-label="'选择用例 ' + item.name"
            />
            <h2>{{ item.name }}</h2>
            <span v-if="item.last_status" class="status" :class="item.last_status">
              <component :is="statusIcon(item.last_status)" :size="13" weight="bold" />
              {{ statusText(item.last_status) }}
            </span>
          </div>
          <p class="meta">
            来源运行
            <RouterLink :to="{ name: 'run-detail', params: { id: item.source_run_id } }" class="mono">
              {{ shortId(item.source_run_id) }}
            </RouterLink>
            <span class="sep">/</span>从第 {{ item.from_seq }} 步回放
            <!-- 只有显式声明 regress 才是回归模式：没声明 preset 的用例按复现语义执行。 -->
            <span class="sep">/</span>{{ item.preset === 'regress' ? '回归模式' : '复现模式' }}
            <span v-if="item.last_run_at" class="sep">/</span>
            <span v-if="item.last_run_at">{{ relativeTime(item.last_run_at) }}执行</span>
            <!-- 结论必须带着前提：没有它，就无法分辨这次结论属于哪个 Prompt 版本。 -->
            <span v-if="item.last_condition" class="sep">/</span>
            <span v-if="item.last_condition">最近一次条件：{{ conditionText(item.last_condition) }}</span>
          </p>
        </header>

        <CausePanel v-if="causeOfCase(item)" :cause="causeOfCase(item)" title="无法判断" compact />

        <div class="body">
          <div class="assertions">
            <span class="block-label">断言</span>
            <ul>
              <li v-for="(assertion, index) in item.assertions" :key="index">
                <span class="assertion-text">{{ describeAssertion(assertion) }}</span>
                <template v-if="item.last_results[index]">
                  <span class="result" :class="{ pass: item.last_results[index].passed }">
                    {{ item.last_results[index].passed ? '通过' : '失败' }}
                  </span>
                  <span class="result-detail">{{ item.last_results[index].detail }}</span>
                </template>
              </li>
            </ul>
          </div>

          <div class="actions">
            <label v-if="Object.keys(presetsFor(item)).length" class="prompt-field">
              <span>以哪个 Prompt 运行</span>
              <select v-model="promptChoice[item.id]">
                <option value="">沿用当前默认</option>
                <option v-for="(_, name) in presetsFor(item)" :key="name" :value="name">{{ name }}</option>
              </select>
            </label>
            <button class="primary" type="button" :disabled="busyCase === item.id" @click="run(item.id)">
              <PhPlay :size="13" weight="bold" />
              {{ busyCase === item.id ? '正在提交' : '运行用例' }}
            </button>
            <div class="budget-field">
              <span class="budget-label">预算上限（可选，留空表示不设上限）</span>
              <div class="budget-inputs">
                <label>
                  <span>最大成本（USD）</span>
                  <input
                    type="text"
                    inputmode="decimal"
                    placeholder="不设上限"
                    :value="budgetInput(item.id).cost"
                    @input="setBudgetField(item.id, 'cost', ($event.target as HTMLInputElement).value)"
                  />
                </label>
                <label>
                  <span>最大模型调用次数</span>
                  <input
                    type="text"
                    inputmode="numeric"
                    placeholder="不设上限"
                    :value="budgetInput(item.id).calls"
                    @input="setBudgetField(item.id, 'calls', ($event.target as HTMLInputElement).value)"
                  />
                </label>
              </div>
              <span v-if="budgetErrorFor(item.id)" class="budget-error">{{ budgetErrorFor(item.id) }}</span>
            </div>
            <RouterLink
              v-if="item.last_run_id"
              class="ghost"
              :to="{ name: 'run-detail', params: { id: item.last_run_id } }"
            >
              查看最近一次执行
            </RouterLink>
          </div>
        </div>
      </li>
    </ul>
  </template>
</template>

<style scoped>
.head {
  margin-bottom: 18px;
}
.lede {
  margin-top: 6px;
  color: var(--text-muted);
  max-width: 82ch;
}
.block-label {
  font-size: 11px;
  color: var(--text-faint);
}

/* ---------------------------------------------------------------- 批量运行 */

.batch {
  border: 1px solid var(--border);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  padding: 14px 16px;
  margin-bottom: 18px;
  display: grid;
  gap: 12px;
  justify-items: start;
}
.batch h2 {
  font-size: var(--step-3);
}
.hint {
  margin-top: 6px;
  font-size: var(--step-1);
  color: var(--text-muted);
  max-width: 90ch;
}
.picker {
  display: grid;
  gap: 12px;
  width: 100%;
}
.selection {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.count {
  font-size: var(--step-1);
  color: var(--text-muted);
}
.conditions {
  display: grid;
  gap: 7px;
}
.condition-row {
  display: flex;
  gap: 7px;
  align-items: center;
}
.condition-row select,
.condition-row input {
  background: var(--bg-inset);
  color: var(--text);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-control);
  padding: 6px 9px;
  font-size: var(--step-1);
  min-width: 190px;
}
.suite-error {
  font-size: var(--step-1);
  color: var(--danger);
}
.submit-row {
  display: flex;
  gap: 9px;
  align-items: center;
  flex-wrap: wrap;
}
.estimate {
  font-size: var(--step-1);
  color: var(--text-muted);
  max-width: 90ch;
}
.budget-hint {
  font-size: 11px;
  color: var(--text-faint);
}
.batch .budget-field {
  width: min(100%, 480px);
}

/* ---------------------------------------------------------------- 批次汇总 */

.suite {
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  padding: 14px 16px;
  margin-bottom: 18px;
}
.suite > header h2 {
  font-size: var(--step-4);
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.suite .meta {
  margin-top: 5px;
  font-size: var(--step-1);
  color: var(--text-muted);
}
/* 整批的预算记账：与单次回放同一套字段，因此读法也一致。 */
.budget-line {
  margin-top: 5px;
  font-size: var(--step-1);
  color: var(--text-muted);
}
/* 触顶不是失败：用中性强调，不借失败色。 */
.budget-hit {
  color: var(--text);
  font-weight: 600;
}
.budget-detail {
  margin-top: 3px;
  font-size: 11px;
  color: var(--text-faint);
  max-width: 90ch;
}
.has-errors {
  color: var(--danger);
}
.groups {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: grid;
  gap: 12px;
}
.groups > li {
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  background: var(--bg-inset);
  padding: 11px 13px;
}
.group-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.group-head h3 {
  font-size: var(--step-2);
}
.progress {
  font-size: 11px;
  color: var(--text-faint);
}
.verdict-line {
  margin-top: 6px;
  font-size: var(--step-1);
  color: var(--text);
}
.counts {
  list-style: none;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 9px 0 0;
  padding: 0;
  font-size: 11px;
}
.counts li {
  border: 1px solid currentColor;
  border-radius: var(--radius-chip);
  padding: 2px 8px;
}
.counts .passed {
  color: var(--success);
}
.counts .failed {
  color: var(--danger);
}
/* 「无法判断」不是失败：琥珀色，与未通过明确分开。 */
.counts .inconclusive {
  color: var(--warning);
}
/* 「执行出错」是另一类：同样没有结论，但没有结论的原因不在断言上，因此用虚框。 */
.counts .error {
  color: var(--danger);
  border-style: dashed;
}
.counts .pending {
  color: var(--text-faint);
}
/* 「未启动」是未完成里的一类：它不是结论，因此不用任何结论色。 */
.counts .not-started {
  color: var(--text-muted);
  border-style: dashed;
}
.items {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
  font-size: var(--step-1);
}
.items th {
  text-align: left;
  font-weight: 500;
  font-size: 11px;
  color: var(--text-faint);
  padding: 0 8px 5px 0;
}
.items td {
  padding: 6px 8px 6px 0;
  border-top: 1px solid var(--border);
  vertical-align: top;
}
.links {
  white-space: nowrap;
}
.diagnosis {
  color: var(--text-muted);
}
.drill {
  margin-top: 10px;
}
.drill summary {
  cursor: pointer;
  font-size: 11px;
  color: var(--text-faint);
}
.drill-body {
  display: grid;
  gap: 10px;
  margin-top: 9px;
}
.drill-item {
  border-top: 1px solid var(--border);
  padding-top: 9px;
  display: grid;
  gap: 6px;
}
.drill-title {
  font-size: var(--step-1);
  display: flex;
  gap: 9px;
  align-items: center;
}
.drill-results {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 4px;
}
.drill-results li {
  display: flex;
  gap: 8px;
  font-size: 11px;
  flex-wrap: wrap;
}

/* ---------------------------------------------------------------- 用例列表 */

.cases {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 12px;
}
.cases > li {
  border: 1px solid var(--border);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  padding: 14px 16px;
}
.cases > li.highlighted {
  border-color: var(--accent-ring);
}
.title-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.title-row h2 {
  font-size: var(--step-3);
}
.pick {
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
  cursor: pointer;
}
.status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 500;
}
.status.passed {
  color: var(--success);
}
.status.failed,
.status.error {
  color: var(--danger);
}
.status.running {
  color: var(--accent);
}
.status.pending {
  color: var(--text-faint);
}
/* 「拿不到结论」不再是失败色：它与「未通过」是两回事，列表里必须一眼可分。 */
.status.inconclusive {
  color: var(--warning);
}
.meta {
  margin-top: 5px;
  font-size: var(--step-1);
  color: var(--text-muted);
}
.sep {
  margin: 0 7px;
  color: var(--text-faint);
}
.body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 200px;
  gap: 16px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  align-items: start;
}
.assertions ul {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: grid;
  gap: 5px;
}
.assertions li {
  display: flex;
  align-items: baseline;
  gap: 9px;
  flex-wrap: wrap;
  font-size: var(--step-1);
}
.assertion-text {
  color: var(--text);
}
.result {
  font-size: 10.5px;
  color: var(--danger);
}
.result.pass {
  color: var(--success);
}
.result-detail {
  font-size: 10.5px;
  color: var(--text-faint);
}
.actions {
  display: grid;
  gap: 8px;
}
.prompt-field {
  display: grid;
  gap: 4px;
}
.prompt-field > span {
  font-size: 11px;
  color: var(--text-faint);
}
.prompt-field select {
  width: 100%;
  background: var(--bg-inset);
  color: var(--text);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-control);
  padding: 6px 9px;
  font-size: var(--step-1);
}
.budget-field {
  display: grid;
  gap: 4px;
}
.budget-label {
  font-size: 11px;
  color: var(--text-faint);
}
.budget-inputs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.budget-inputs label {
  display: grid;
  gap: 4px;
}
.budget-inputs label > span {
  font-size: 10px;
  color: var(--text-faint);
}
.budget-inputs input {
  width: 100%;
  background: var(--bg-inset);
  color: var(--text);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-control);
  padding: 6px 9px;
  font-size: var(--step-1);
}
.budget-error {
  font-size: 11px;
  color: var(--danger);
}
.primary,
.ghost {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 13px;
  border-radius: var(--radius-control);
  cursor: pointer;
  font-size: var(--step-1);
  white-space: nowrap;
}
.primary {
  border: 1px solid var(--accent-ring);
  background: var(--accent-dim);
  color: var(--accent-strong);
}
.primary:hover:not(:disabled) {
  background: rgba(76, 194, 212, 0.22);
}
.primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.ghost {
  border: 1px solid var(--border-strong);
  color: var(--text-muted);
  background: transparent;
}
.ghost:hover:not(:disabled) {
  color: var(--text);
  background: var(--bg-hover);
}
.ghost:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.ghost.icon {
  padding: 7px 9px;
}
.ghost.add {
  justify-self: start;
}
.primary:active:not(:disabled),
.ghost:active:not(:disabled) {
  transform: translateY(1px);
}
@media (max-width: 820px) {
  .body {
    grid-template-columns: minmax(0, 1fr);
  }
  .condition-row {
    flex-wrap: wrap;
  }
}
</style>
