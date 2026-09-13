<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { PhTarget } from '@phosphor-icons/vue'
import { api } from '@/api/client'
import type { AlignedItem, RunDiff, RunListItem } from '@/api/types'
import StatePanel from '@/components/StatePanel.vue'
import { FORK_LABELS, inlineJson, shortId, truncate } from '@/utils/format'

const route = useRoute()
const router = useRouter()

const runs = ref<RunListItem[]>([])
const diff = ref<RunDiff | null>(null)
const loading = ref(false)
const error = ref('')
const a = ref(String(route.query.a ?? ''))
const b = ref(String(route.query.b ?? ''))

const ready = computed(() => Boolean(a.value && b.value))

async function loadRuns() {
  try {
    const response = await api.runs({ limit: 200 })
    runs.value = response.runs
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}

async function loadDiff() {
  if (!ready.value) return
  loading.value = true
  error.value = ''
  try {
    diff.value = await api.diff(a.value, b.value)
    router.replace({ name: 'diff', query: { a: a.value, b: b.value } })
  } catch (cause) {
    diff.value = null
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

function label(item: RunListItem) {
  const replay = item.is_replay ? ' · 回放' : ''
  return `${item.run.agent_name} · ${item.run.model ?? '未标注'} · ${shortId(item.run.id, 6)}${replay}`
}

function tone(item: AlignedItem) {
  return item.status
}

function statusText(status: string) {
  if (status === 'same') return '一致'
  if (status === 'changed') return '有差异'
  if (status === 'only_a') return '仅基准有'
  return '仅对照有'
}

watch([a, b], () => {
  if (ready.value) loadDiff()
})

onMounted(async () => {
  await loadRuns()
  if (ready.value) await loadDiff()
})
</script>

<template>
  <section class="head">
    <div>
      <h1>运行对比</h1>
      <p class="lede">
        把两次运行对齐到同一条时间轴上，看清楚工具调用、模型输出与成本的差异究竟发生在哪一步。
      </p>
    </div>
  </section>

  <section class="picker">
    <label class="field">
      <span>基准运行</span>
      <select v-model="a">
        <option value="">选择一次运行</option>
        <option v-for="item in runs" :key="item.run.id" :value="item.run.id">{{ label(item) }}</option>
      </select>
    </label>
    <label class="field">
      <span>对照运行</span>
      <select v-model="b">
        <option value="">选择一次运行</option>
        <option v-for="item in runs" :key="item.run.id" :value="item.run.id">{{ label(item) }}</option>
      </select>
    </label>
  </section>

  <StatePanel v-if="loading" variant="loading" />
  <StatePanel v-else-if="error" variant="error" title="无法完成对比" :detail="error" @retry="loadDiff" />
  <StatePanel
    v-else-if="!ready"
    variant="empty"
    title="选择两次运行开始对比"
    detail="在运行记录页勾选两条并点击「对比所选」，或者在上面的下拉框里各选一条。"
  />
  <StatePanel
    v-else-if="!diff"
    variant="empty"
    title="没有可显示的差异"
    detail="这两次运行完全一致。"
  />

  <template v-else>
    <section class="verdict" :class="{ changed: diff.final_output.changed }">
      <p class="verdict-text">{{ diff.verdict }}</p>
      <p class="verdict-pair mono">{{ diff.a_label }} → {{ diff.b_label }}</p>
    </section>

    <section v-if="diff.first_divergence" class="divergence">
      <PhTarget :size="16" weight="bold" class="divergence-icon" />
      <div>
        <p class="divergence-title">
          第一个不同的步骤：{{ FORK_LABELS[diff.first_divergence.kind] ?? diff.first_divergence.kind }}
        </p>
        <p class="divergence-detail">
          {{ diff.first_divergence.message }}
          <template v-if="diff.first_divergence.parent_seq">
            （基准第 {{ diff.first_divergence.parent_seq }} 步）
          </template>
        </p>
      </div>
    </section>

    <div class="two-col">
      <section class="card">
        <h2>指标变化</h2>
        <ul class="deltas">
          <li v-for="item in diff.summary" :key="item.field">
            <span class="delta-label">{{ item.field }}</span>
            <span class="delta-values mono">{{ item.a }} → {{ item.b }}</span>
            <span class="delta-chip" :class="item.hint">{{ item.hint || ' ' }}</span>
          </li>
        </ul>
      </section>

      <section class="card">
        <h2>最终结论</h2>
        <p v-if="!diff.final_output.changed" class="same-note">两次运行的最终结论一致。</p>
        <div class="final-grid">
          <div>
            <span class="final-label">基准</span>
            <pre class="final">{{ diff.final_output.a || '（空）' }}</pre>
          </div>
          <div>
            <span class="final-label">对照</span>
            <pre class="final" :class="{ changed: diff.final_output.changed }">{{
              diff.final_output.b || '（空）'
            }}</pre>
          </div>
        </div>
      </section>
    </div>

    <section class="card">
      <h2>工具调用对齐</h2>
      <ul class="align">
        <li v-for="item in diff.tools" :key="item.index" :class="tone(item)">
          <div class="align-head">
            <span class="align-index mono">{{ item.index + 1 }}</span>
            <span class="align-label mono">{{ item.label }}</span>
            <span class="align-status">{{ statusText(item.status) }}</span>
          </div>
          <div class="align-body">
            <div class="align-side">
              <span class="align-source">基准</span>
              <code v-if="item.a">{{ truncate(inlineJson(item.a.args), 200) }}</code>
              <span v-else class="align-missing">不存在</span>
              <span v-if="item.a?.effect_source" class="align-effect">{{ item.a.effect_source }}</span>
            </div>
            <div class="align-side">
              <span class="align-source">对照</span>
              <code v-if="item.b">{{ truncate(inlineJson(item.b.args), 200) }}</code>
              <span v-else class="align-missing">不存在</span>
              <span v-if="item.b?.effect_source" class="align-effect">{{ item.b.effect_source }}</span>
            </div>
          </div>
        </li>
      </ul>
      <p v-if="!diff.tools.length" class="same-note">两次运行都没有工具调用。</p>
    </section>

    <section class="card">
      <h2>模型调用对齐</h2>
      <ul class="align">
        <li v-for="item in diff.models" :key="item.index" :class="tone(item)">
          <div class="align-head">
            <span class="align-index mono">{{ item.index + 1 }}</span>
            <span class="align-label mono">{{ item.label }}</span>
            <span class="align-status">{{ statusText(item.status) }}</span>
            <span v-if="item.detail.text_changed" class="flag">文本不同</span>
            <span v-if="item.detail.tool_calls_changed" class="flag">工具选择不同</span>
          </div>
          <div class="align-body">
            <pre class="align-text">{{ truncate(String(item.a?.text ?? ''), 260) || '（空）' }}</pre>
            <pre class="align-text">{{
              item.b ? truncate(String(item.b.text ?? ''), 260) || '（空）' : '不存在'
            }}</pre>
          </div>
        </li>
      </ul>
    </section>
  </template>
</template>

<style scoped>
.head {
  margin-bottom: 16px;
}
.lede {
  margin-top: 6px;
  color: var(--text-muted);
  max-width: 78ch;
}
.picker {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 18px;
}
.field {
  display: grid;
  gap: 4px;
}
.field > span {
  font-size: 11px;
  color: var(--text-faint);
}
select {
  width: 100%;
  background: var(--bg-raised);
  color: var(--text);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-control);
  padding: 8px 10px;
}

.verdict {
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--recorded);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  padding: 14px 16px;
}
.verdict.changed {
  border-left-color: var(--accent);
}
.verdict-text {
  font-size: var(--step-3);
  font-weight: 600;
}
.verdict-pair {
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-faint);
}

.divergence {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid rgba(224, 164, 88, 0.3);
  border-radius: var(--radius-container);
  background: var(--warning-dim);
}
.divergence-icon {
  color: var(--warning);
  margin-top: 2px;
}
.divergence-title {
  font-weight: 600;
}
.divergence-detail {
  color: var(--text-muted);
  font-size: var(--step-1);
}

.two-col {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: 16px;
  margin-top: 16px;
}
.card {
  border: 1px solid var(--border);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  padding: 14px 16px;
  margin-top: 16px;
}
.two-col .card {
  margin-top: 0;
}
.card h2 {
  font-size: var(--step-3);
  margin-bottom: 10px;
}

.deltas {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 9px;
}
.deltas li {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 10px;
  align-items: baseline;
}
.delta-label {
  color: var(--text-muted);
  font-size: var(--step-1);
}
.delta-values {
  font-size: var(--step-1);
}
.delta-chip {
  font-size: 10.5px;
  color: var(--text-faint);
}
.delta-chip.更好 {
  color: var(--success);
}
.delta-chip.更差 {
  color: var(--warning);
}

.final-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.final-label {
  font-size: 11px;
  color: var(--text-faint);
}
.final {
  margin: 4px 0 0;
  padding: 10px 12px;
  background: var(--bg-inset);
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  font-size: var(--step-1);
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 360px;
  overflow: auto;
}
.final.changed {
  border-color: var(--accent-ring);
}
.same-note {
  color: var(--text-muted);
  font-size: var(--step-1);
}

.align {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 8px;
}
.align li {
  border: 1px solid var(--border);
  border-left: 3px solid var(--border-strong);
  border-radius: var(--radius-control);
  background: var(--bg-inset);
  padding: 9px 12px;
}
.align li.changed {
  border-left-color: var(--accent);
}
.align li.only_a,
.align li.only_b {
  border-left-color: var(--warning);
}
.align-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.align-index {
  font-size: 10.5px;
  color: var(--text-faint);
}
.align-label {
  font-size: var(--step-1);
  color: var(--text);
}
.align-status {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--text-muted);
}
.flag {
  font-size: 10.5px;
  color: var(--accent);
  border: 1px solid var(--accent-ring);
  border-radius: var(--radius-chip);
  padding: 0 6px;
}
.align-body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 7px;
}
.align-side {
  display: grid;
  gap: 3px;
  min-width: 0;
}
.align-source {
  font-size: 10.5px;
  color: var(--text-faint);
}
.align-side code {
  font-size: 11px;
  color: var(--text-muted);
  word-break: break-word;
}
.align-missing {
  font-size: 11px;
  color: var(--text-faint);
}
.align-effect {
  font-size: 10.5px;
  color: var(--text-faint);
}
.align-text {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 180px;
  overflow: auto;
}
@media (max-width: 1000px) {
  .picker,
  .two-col,
  .final-grid,
  .align-body {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
