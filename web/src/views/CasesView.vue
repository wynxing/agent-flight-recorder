<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { PhCheckCircle, PhPlay, PhQuestion, PhXCircle } from '@phosphor-icons/vue'
import { api } from '@/api/client'
import type { CaseItem } from '@/api/types'
import CausePanel from '@/components/CausePanel.vue'
import StatePanel from '@/components/StatePanel.vue'
import { useSessionStore } from '@/stores/session'
import { ASSERTION_LABELS, causeOf, relativeTime, shortId } from '@/utils/format'

const route = useRoute()
const session = useSessionStore()

const cases = ref<CaseItem[]>([])
const loading = ref(true)
const error = ref('')
const busyCase = ref('')
const promptChoice = ref<Record<string, string>>({})

let poller: number | null = null

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await api.cases()
    cases.value = response.cases
    if (response.cases.some((item) => item.last_status === 'running')) startPolling()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
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

function presetsFor(item?: CaseItem) {
  if (!item?.source_run) return {}
  const agent = session.agents.find((entry) => entry.name === item.source_run?.agent_name)
  return agent?.prompt_presets ?? {}
}

function describeAssertion(assertion: { type: string; tool?: string | null; value?: unknown }) {
  const label = ASSERTION_LABELS[assertion.type] ?? assertion.type
  const target = assertion.tool ?? (assertion.value !== undefined && assertion.value !== null ? String(assertion.value) : '')
  return target ? `${label}: ${target}` : label
}

function isHighlighted(caseId: string) {
  return route.query.highlight === caseId
}

// 「拿不到结论」与「结论是不通过」是两件事，不能共用一个叉。
// 前者用问号 + 琥珀色，后者才是表示失败的叉。
const STATUS_ICONS: Record<string, unknown> = {
  passed: PhCheckCircle,
  running: PhPlay,
  inconclusive: PhQuestion,
  failed: PhXCircle,
  error: PhXCircle,
}

function statusIcon(status: string) {
  return STATUS_ICONS[status] ?? PhXCircle
}

function causeOfCase(item: CaseItem) {
  return item.last_status === 'inconclusive' ? causeOf(item.last_cause) : null
}

onMounted(async () => {
  await session.loadAgents()
  await load()
})
onUnmounted(stopPolling)
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

  <ul v-else class="cases">
    <li v-for="item in cases" :key="item.id" :class="{ highlighted: isHighlighted(item.id) }">
      <header>
        <div class="title-row">
          <h2>{{ item.name }}</h2>
          <span v-if="item.last_status" class="status" :class="item.last_status">
            <component :is="statusIcon(item.last_status)" :size="13" weight="bold" />
            {{
              item.last_status === 'passed'
                ? '通过'
                : item.last_status === 'running'
                  ? '正在执行'
                  : item.last_status === 'error'
                    ? '执行出错'
                    : item.last_status === 'inconclusive' ? '无法判断' : '未通过'
            }}
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

<style scoped>
.head {
  margin-bottom: 18px;
}
.lede {
  margin-top: 6px;
  color: var(--text-muted);
  max-width: 82ch;
}
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
.block-label {
  font-size: 11px;
  color: var(--text-faint);
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
}
.ghost:hover {
  color: var(--text);
  background: var(--bg-hover);
}
.primary:active:not(:disabled),
.ghost:active {
  transform: translateY(1px);
}
@media (max-width: 820px) {
  .body {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
