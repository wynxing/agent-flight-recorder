<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { PhArrowsLeftRight, PhArrowClockwise, PhPlay } from '@phosphor-icons/vue'
import { api } from '@/api/client'
import type { RunListItem } from '@/api/types'
import StatePanel from '@/components/StatePanel.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatDuration, formatNumber, relativeTime, shortId } from '@/utils/format'

const router = useRouter()

const runs = ref<RunListItem[]>([])
const loading = ref(true)
const error = ref('')
const agentFilter = ref('')
const statusFilter = ref('')
const rootsOnly = ref(false)
const selected = ref<string[]>([])
const launching = ref(false)
const launchMessage = ref('')

const agentOptions = computed(() => {
  const names = new Set(runs.value.map((item) => item.run.agent_name))
  return Array.from(names).sort()
})

const canCompare = computed(() => selected.value.length === 2)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await api.runs({
      limit: 200,
      agent_name: agentFilter.value || undefined,
      status: statusFilter.value || undefined,
      roots_only: rootsOnly.value,
    })
    runs.value = response.runs
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

function toggleSelect(runId: string) {
  const index = selected.value.indexOf(runId)
  if (index >= 0) {
    selected.value.splice(index, 1)
    return
  }
  selected.value.push(runId)
  if (selected.value.length > 2) selected.value.shift()
}

function compare() {
  if (!canCompare.value) return
  router.push({ name: 'diff', query: { a: selected.value[0], b: selected.value[1] } })
}

async function runDemo() {
  launching.value = true
  launchMessage.value = ''
  try {
    const agents = await api.agents()
    if (!agents.length) {
      launchMessage.value = '没有注册的示例 Agent。请先安装 examples/langgraph_sre_agent。'
      return
    }
    launchMessage.value = '示例 Agent 已注册。在终端运行下面的命令可以产生新的运行记录：'
  } catch (cause) {
    launchMessage.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    launching.value = false
  }
}

function open(runId: string) {
  router.push({ name: 'run-detail', params: { id: runId } })
}

onMounted(load)
</script>

<template>
  <section class="head">
    <div>
      <h1>运行记录</h1>
      <p class="lede">
        每一次 Agent 执行都完整落在时间线上。选中两次运行可以并排比较它们在工具调用、模型输出与成本上的差异。
      </p>
    </div>
    <div class="head-actions">
      <button class="ghost" type="button" @click="load">
        <PhArrowClockwise :size="14" weight="bold" />
        刷新
      </button>
      <button class="primary" type="button" :disabled="!canCompare" @click="compare">
        <PhArrowsLeftRight :size="14" weight="bold" />
        对比所选
      </button>
    </div>
  </section>

  <section class="filters">
    <label class="field">
      <span>Agent</span>
      <select v-model="agentFilter" @change="load">
        <option value="">全部</option>
        <option v-for="name in agentOptions" :key="name" :value="name">{{ name }}</option>
      </select>
    </label>
    <label class="field">
      <span>状态</span>
      <select v-model="statusFilter" @change="load">
        <option value="">全部</option>
        <option value="succeeded">成功</option>
        <option value="failed">失败</option>
        <option value="running">进行中</option>
      </select>
    </label>
    <label class="toggle">
      <input v-model="rootsOnly" type="checkbox" @change="load" />
      <span>只看原始运行</span>
    </label>
    <span class="count">共 {{ runs.length }} 条</span>
  </section>

  <StatePanel v-if="loading" variant="loading" />

  <StatePanel
    v-else-if="error"
    variant="error"
    title="无法读取运行记录"
    :detail="error"
    @retry="load"
  />

  <StatePanel
    v-else-if="!runs.length"
    variant="empty"
    title="还没有任何运行记录"
    detail="启动服务端时会自动跑一次示例 Agent。也可以在终端执行 python -m sre_agent.run 手动产生一条记录。"
  />

  <div v-else class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="pick" />
          <th>状态</th>
          <th>Agent</th>
          <th>模型</th>
          <th class="num">步骤</th>
          <th class="num">工具</th>
          <th class="num">token</th>
          <th class="num">耗时</th>
          <th>开始时间</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in runs"
          :key="item.run.id"
          :class="{ picked: selected.includes(item.run.id) }"
          @click="open(item.run.id)"
        >
          <td class="pick" @click.stop="toggleSelect(item.run.id)">
            <input type="checkbox" :checked="selected.includes(item.run.id)" aria-label="选择用于对比" />
          </td>
          <td><StatusBadge :status="item.run.status" /></td>
          <td>
            <span class="agent">{{ item.run.agent_name }}</span>
            <span v-if="item.is_replay" class="tag replay">回放自第 {{ item.run.replay_from_seq }} 步</span>
            <span v-else-if="item.run.labels?.seed" class="tag seed">示例数据</span>
            <span class="id mono">{{ shortId(item.run.id) }}</span>
          </td>
          <td class="mono muted">{{ item.run.model || '-' }}</td>
          <td class="num mono">{{ item.summary.event_count }}</td>
          <td class="num mono">{{ item.summary.tool_calls }}</td>
          <td class="num mono">{{ formatNumber(item.summary.tokens_total) }}</td>
          <td class="num mono">{{ formatDuration(item.summary.duration_ms) }}</td>
          <td class="muted">{{ relativeTime(item.run.started_at) }}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <section class="launcher">
    <div class="launcher-head">
      <PhPlay :size="15" weight="bold" class="launcher-icon" />
      <span>产生新的运行记录</span>
      <button class="ghost small" type="button" :disabled="launching" @click="runDemo">检查示例 Agent</button>
    </div>
    <pre class="command mono">python -m sre_agent.run --prompt default
python -m sre_agent.run --prompt grounded</pre>
    <p v-if="launchMessage" class="launcher-note">{{ launchMessage }}</p>
  </section>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 18px;
}
.lede {
  margin-top: 6px;
  color: var(--text-muted);
  max-width: 78ch;
}
.head-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.ghost,
.primary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 13px;
  border-radius: var(--radius-control);
  cursor: pointer;
  font-size: var(--step-1);
  white-space: nowrap;
  transition: background 120ms ease, border-color 120ms ease;
}
.ghost {
  border: 1px solid var(--border-strong);
  background: transparent;
  color: var(--text-muted);
}
.ghost:hover {
  color: var(--text);
  background: var(--bg-hover);
}
.primary {
  border: 1px solid transparent;
  background: var(--accent-dim);
  color: var(--accent-strong);
  border-color: var(--accent-ring);
}
.primary:hover:not(:disabled) {
  background: rgba(76, 194, 212, 0.22);
}
.primary:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.ghost:active,
.primary:active:not(:disabled) {
  transform: translateY(1px);
}
.small {
  padding: 4px 10px;
  font-size: 11px;
}

.filters {
  display: flex;
  align-items: flex-end;
  gap: 16px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field span {
  font-size: 11px;
  color: var(--text-faint);
}
select {
  background: var(--bg-raised);
  color: var(--text);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-control);
  padding: 6px 10px;
  min-width: 150px;
}
.toggle {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--text-muted);
  font-size: var(--step-1);
  padding-bottom: 7px;
  cursor: pointer;
}
.count {
  margin-left: auto;
  color: var(--text-faint);
  font-size: var(--step-1);
  padding-bottom: 7px;
}

.table-wrap {
  border: 1px solid var(--border);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  overflow: hidden;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th {
  text-align: left;
  font-size: 11px;
  font-weight: 500;
  color: var(--text-faint);
  padding: 10px 14px;
}
td {
  padding: 11px 14px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
tbody tr:last-child td {
  border-bottom: 0;
}
tbody tr {
  cursor: pointer;
  transition: background 100ms ease;
}
tbody tr:hover {
  background: var(--bg-hover);
}
tbody tr.picked {
  background: var(--accent-dim);
}
.num {
  text-align: right;
}
.pick {
  width: 38px;
  text-align: center;
}
.muted {
  color: var(--text-muted);
}
.agent {
  color: var(--text);
}
.id {
  margin-left: 8px;
  font-size: 11px;
  color: var(--text-faint);
}
.tag {
  margin-left: 8px;
  font-size: 10.5px;
  padding: 1px 6px;
  border-radius: var(--radius-chip);
}
.tag.replay {
  color: var(--accent);
  background: var(--accent-dim);
}
.tag.seed {
  color: var(--text-faint);
  background: var(--bg-hover);
}

.launcher {
  margin-top: 22px;
  border: 1px solid var(--border);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  padding: 14px 16px;
}
.launcher-head {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: var(--step-1);
}
.launcher-icon {
  color: var(--accent);
}
.launcher-head button {
  margin-left: auto;
}
.command {
  margin: 10px 0 0;
  padding: 10px 12px;
  background: var(--bg-inset);
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  font-size: var(--step-1);
  color: var(--text);
  overflow-x: auto;
}
.launcher-note {
  margin-top: 8px;
  font-size: var(--step-1);
  color: var(--warning);
}

@media (max-width: 900px) {
  .head {
    flex-direction: column;
  }
  .table-wrap {
    overflow-x: auto;
  }
  table {
    min-width: 820px;
  }
}
</style>

