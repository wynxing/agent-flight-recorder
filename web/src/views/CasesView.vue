<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  PhCheckCircle,
  PhClock,
  PhPlay,
  PhPlus,
  PhQuestion,
  PhTrash,
  PhXCircle,
} from '@phosphor-icons/vue'
import { api } from '@/api/client'
import type {
  CaseItem,
  SuiteCondition,
  SuiteConditionGroup,
  SuiteConditionInfo,
  SuiteDetail,
  SuiteItem,
  SuiteItemStatus,
} from '@/api/types'
import CausePanel from '@/components/CausePanel.vue'
import StatePanel from '@/components/StatePanel.vue'
import { useSessionStore } from '@/stores/session'
import { ASSERTION_LABELS, causeInfo, causeOf, relativeTime, shortId, truncate } from '@/utils/format'
import { causeDrillLabel, conditionLabel, conditionVerdict, determinableRateText } from '@/utils/suite'

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
  try {
    const presetName = promptChoice.value[caseId] ?? ''
    const item = cases.value.find((entry) => entry.id === caseId)
    const presets = presetsFor(item)
    const systemPrompt = presetName ? presets[presetName] : undefined
    // 选了 Prompt 版本就意味着要真的跑模型，因此固定按回归模式执行。
    await api.runCase(caseId, {
      system_prompt: systemPrompt,
      preset: systemPrompt ? 'regress' : undefined,
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
  submitting.value = true
  try {
    // 全选时走「全部用例」这条路，让服务端的这个入口也真的被用到。
    const response = allSelected.value
      ? await api.runSuite({ all_cases: true, conditions: chosenConditions() })
      : await api.runSuite({ case_ids: ids, conditions: chosenConditions() })
    await loadSuite(response.suite_id)
    startPolling()
  } catch (cause) {
    suiteError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    submitting.value = false
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
  passed: '通过',
  inconclusive: '无法判断',
  failed: '未通过',
  error: '执行出错',
}

function statusText(status?: string | null) {
  if (!status) return ''
  return STATUS_TEXTS[status as SuiteItemStatus] ?? status
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

      <p v-if="allSelected" class="hint">已全选，将按「全部用例」提交。</p>
      <p v-if="suiteError" class="suite-error">{{ suiteError }}</p>

      <button class="primary" type="button" :disabled="submitting" @click="startSuite">
        <PhPlay :size="13" weight="bold" />
        {{ submitting ? '正在提交' : '发起批量运行' }}
      </button>
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
