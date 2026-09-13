<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { PhArrowsLeftRight, PhArrowClockwise, PhFunnelSimple, PhPlus, PhTestTube } from '@phosphor-icons/vue'
import { api, streamRun } from '@/api/client'
import type { AgentEvent, EffectMode, ReplayPreset, RunDetailResponse } from '@/api/types'
import { useSessionStore } from '@/stores/session'
import MetricStrip from '@/components/MetricStrip.vue'
import StatePanel from '@/components/StatePanel.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import StepRow from '@/components/StepRow.vue'
import { formatTime, shortId, textOf } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()

const runId = computed(() => String(route.params.id))
const detail = ref<RunDetailResponse | null>(null)
const events = ref<AgentEvent[]>([])
const loading = ref(true)
const error = ref('')
const showSnapshots = ref(false)
const streaming = ref(false)

const fromSeq = ref(1)
const preset = ref<ReplayPreset>('regress')
const modelOverride = ref('')
const promptOverride = ref('')
const promptPreset = ref('')
const allowSideEffects = ref(false)
const stepOverrides = ref<Record<number, EffectMode>>({})
const showAdvanced = ref(false)
const replayBusy = ref(false)
const replayError = ref('')
const replayLaunched = ref<{ run_id: string; plan_summary: string } | null>(null)

const caseName = ref('')
const caseContains = ref('')
const caseBusy = ref(false)
const caseError = ref('')

let closeStream: (() => void) | null = null

const behavioral = computed(() =>
  events.value.filter((event) => event.type === 'model_call' || event.type === 'tool_call'),
)

const visibleEvents = computed(() =>
  showSnapshots.value ? events.value : events.value.filter((event) => event.type !== 'state_snapshot'),
)

const snapshotCount = computed(() => events.value.filter((event) => event.type === 'state_snapshot').length)

const agentInfo = computed(() => session.agents.find((item) => item.name === detail.value?.run.agent_name))
const isPi = computed(() => detail.value?.run.metadata.runtime === 'pi')

const promptPresets = computed(() => agentInfo.value?.prompt_presets ?? {})

const toolNames = computed(() => {
  const names = new Set(events.value.filter((e) => e.type === 'tool_call' && e.name).map((e) => e.name as string))
  return Array.from(names)
})

const conclusion = computed(() => {
  const finished = [...events.value].reverse().find((event) => event.type === 'run_finished')
  const result = finished?.output?.result
  if (typeof result === 'string' && result.trim()) return result
  const models = events.value.filter((event) => event.type === 'model_call')
  const last = models[models.length - 1]
  return last ? textOf(last) : '这次运行还没有产生结论。'
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [meta, timeline] = await Promise.all([api.run(runId.value), api.timeline(runId.value)])
    detail.value = meta
    events.value = timeline.events
    const last = timeline.events.filter((e) => e.type === 'model_call')
    fromSeq.value = last.length ? last[last.length - 1].seq : 1
    if (meta.run.status === 'running') startTail()
    await session.loadAgents()
    promptPreset.value = Object.keys(promptPresets.value)[0] ?? ''
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

function startTail() {
  if (closeStream) return
  const after = events.value.length ? events.value[events.value.length - 1].seq : 0
  streaming.value = true
  closeStream = streamRun(runId.value, after, {
    onStep: (event) => {
      if (!events.value.some((item) => item.seq === event.seq)) events.value.push(event)
    },
    onDone: () => {
      streaming.value = false
      closeStream = null
      refreshSummary()
    },
    onError: () => {
      streaming.value = false
      closeStream = null
    },
  })
}

async function refreshSummary() {
  try {
    const meta = await api.run(runId.value)
    detail.value = meta
  } catch {
    // 摘要刷新失败不影响已经渲染的时间线
  }
}

function pickStep(event: AgentEvent) {
  fromSeq.value = event.seq
  showAdvanced.value = true
}

function applyPreset(name: string) {
  promptPreset.value = name
  if (name) promptOverride.value = promptPresets.value[name] ?? ''
}

function setStepMode(seq: number, mode: EffectMode | '') {
  if (!mode) {
    delete stepOverrides.value[seq]
    return
  }
  stepOverrides.value = { ...stepOverrides.value, [seq]: mode }
}

async function startReplay() {
  replayBusy.value = true
  replayError.value = ''
  replayLaunched.value = null
  try {
    const byKind: Record<string, EffectMode> =
      preset.value === 'regress' ? { model_call: 'live' } : {}
    const policy = {
      default: 'recorded' as EffectMode,
      by_kind: byKind,
      by_seq: Object.fromEntries(Object.entries(stepOverrides.value).map(([k, v]) => [k, v])),
      allow_side_effect_execution: allowSideEffects.value,
    }
    const response = await api.replay(runId.value, {
      from_seq: fromSeq.value,
      preset: preset.value,
      policy,
      model: modelOverride.value || undefined,
      system_prompt: promptOverride.value || undefined,
    })
    replayLaunched.value = { run_id: response.run_id, plan_summary: response.plan_summary }
  } catch (cause) {
    replayError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    replayBusy.value = false
  }
}

async function createCase() {
  caseBusy.value = true
  caseError.value = ''
  try {
    const assertions: { type: string; tool?: string; value?: unknown }[] = [{ type: 'no_error' }]
    for (const tool of toolNames.value) assertions.push({ type: 'tool_called', tool })
    if (caseContains.value.trim()) {
      assertions.push({ type: 'final_output_contains', value: caseContains.value.trim() })
    } else {
      assertions.push({ type: 'final_output_not_contains', value: '占位断言请替换' })
    }
    const created = await api.createCase({
      name: caseName.value.trim() || `${detail.value?.run.agent_name ?? 'agent'} 回归用例`,
      description: `来自运行 ${shortId(runId.value)}`,
      source_run_id: runId.value,
      from_seq: fromSeq.value,
      assertions,
      preset: 'regress',
      system_prompt: promptOverride.value || undefined,
    })
    router.push({ name: 'cases', query: { highlight: created.id } })
  } catch (cause) {
    caseError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    caseBusy.value = false
  }
}

function openDiff() {
  const other = detail.value?.run.parent_run_id
  if (other) {
    router.push({ name: 'diff', query: { a: other, b: runId.value } })
    return
  }
  router.push({ name: 'diff', query: { a: runId.value } })
}

onMounted(load)
onUnmounted(() => {
  closeStream?.()
  closeStream = null
})
</script>

<template>
  <StatePanel v-if="loading" variant="loading" />
  <StatePanel v-else-if="error" variant="error" title="无法读取这次运行" :detail="error" @retry="load" />

  <template v-else-if="detail">
    <section class="head">
      <div class="head-main">
        <div class="title-row">
          <h1 class="mono">{{ shortId(detail.run.id, 12) }}</h1>
          <StatusBadge :status="detail.run.status" />
          <span v-if="streaming" class="tailing">正在实时尾随</span>
        </div>
        <p class="meta">
          <span>{{ detail.run.agent_name }}</span>
          <span class="sep">/</span>
          <span class="mono">{{ detail.run.model || '未标注模型' }}</span>
          <span class="sep">/</span>
          <span>{{ formatTime(detail.run.started_at) }}</span>
        </p>
        <p v-if="detail.run.parent_run_id" class="lineage">
          这是一次回放：从第 {{ detail.run.replay_from_seq }} 步开始。
          <RouterLink :to="{ name: 'run-detail', params: { id: detail.run.parent_run_id } }">
            查看原始运行
          </RouterLink>
        </p>
        <p v-if="detail.run.effect_policy?.allow_side_effect_execution" class="danger-note">
          本次回放允许真实执行副作用。请核对时间线上的告警标记。
        </p>
        <p v-if="detail.run.redactions?.length" class="redaction-note">
          入库时命中脱敏规则：{{ detail.run.redactions.join(', ') }}
        </p>
      </div>
      <div class="head-actions">
        <button class="ghost" type="button" @click="load">
          <PhArrowClockwise :size="14" weight="bold" />刷新
        </button>
        <button class="ghost" type="button" @click="openDiff">
          <PhArrowsLeftRight :size="14" weight="bold" />对比
        </button>
      </div>
    </section>

    <MetricStrip :summary="detail.summary" />

    <div class="layout">
      <section class="timeline">
        <header class="section-head">
          <h2>时间线</h2>
          <label class="toggle">
            <input v-model="showSnapshots" type="checkbox" />
            <PhFunnelSimple :size="13" weight="bold" />
            <span>显示 {{ snapshotCount }} 条状态快照</span>
          </label>
        </header>

        <ul class="steps">
          <StepRow
            v-for="event in visibleEvents"
            :key="event.seq"
            :event="event"
            :highlight="event.seq === fromSeq"
            :pinnable="!isPi"
            @pick="pickStep"
          />
        </ul>
      </section>

      <aside class="side">
        <section v-if="detail.replay?.complete === false" class="panel">
          <h2>无法判断</h2>
          <p class="warn-note">录制或执行上下文不完整：{{ detail.replay.reason }}</p>
        </section>
        <section v-if="isPi" class="panel">
          <h2>本地 pi 回放</h2>
          <p class="hint">在 integrations/pi 中运行，替换文件名为本次保存的回放包。仅支持从任务起点回放。</p>
          <code>npm start -- replay --bundle run.json --mode regress --out replay.json</code>
          <h2>创建本地用例</h2>
          <code>npm start -- case create --bundle run.json --assertions assertions.json --out case.json</code>
        </section>
        <section v-if="!isPi" class="panel">
          <h2>回放</h2>
          <p class="hint">
            分叉点之前的步骤一律按录制结果复现，不产生任何真实调用；分叉点之后按下面的策略执行。
          </p>

          <label class="field">
            <span>起始步骤</span>
            <select v-model.number="fromSeq">
              <option v-for="event in behavioral" :key="event.seq" :value="event.seq">
                第 {{ event.seq }} 步 · {{ event.type === 'model_call' ? '模型调用' : event.name }}
              </option>
            </select>
          </label>

          <div class="field">
            <span>模式</span>
            <div class="segmented">
              <button
                type="button"
                :class="{ active: preset === 'reproduce' }"
                @click="preset = 'reproduce'"
              >
                复现
              </button>
              <button type="button" :class="{ active: preset === 'regress' }" @click="preset = 'regress'">
                回归
              </button>
            </div>
            <p class="mode-note">
              {{
                preset === 'reproduce'
                  ? '复现：全部使用录制结果，用来确认第几步开始偏离。'
                  : '回归：模型真实执行，工具沿用录制结果，用来确认改动是否更好。'
              }}
            </p>
          </div>

          <template v-if="preset === 'regress'">
            <label class="field">
              <span>Prompt 版本</span>
              <select :value="promptPreset" @change="applyPreset(($event.target as HTMLSelectElement).value)">
                <option value="">不改动，沿用原 Prompt</option>
                <option v-for="(_, name) in promptPresets" :key="name" :value="name">
                  {{ name }}
                </option>
              </select>
            </label>
            <label class="field">
              <span>System Prompt 覆盖</span>
              <textarea v-model="promptOverride" rows="5" placeholder="留空则沿用原有 Prompt" />
            </label>
            <label class="field">
              <span>模型覆盖</span>
              <input v-model="modelOverride" type="text" placeholder="留空则沿用原模型" />
            </label>
          </template>

          <button class="disclosure" type="button" @click="showAdvanced = !showAdvanced">
            {{ showAdvanced ? '收起单步策略' : `单步策略（已覆盖 ${Object.keys(stepOverrides).length} 步）` }}
          </button>

          <div v-if="showAdvanced" class="advanced">
            <div v-for="event in behavioral" :key="event.seq" class="step-override">
              <span class="step-label mono">
                {{ event.seq }} · {{ event.type === 'model_call' ? '模型' : event.name }}
              </span>
              <select
                :value="stepOverrides[event.seq] ?? ''"
                @change="setStepMode(event.seq, ($event.target as HTMLSelectElement).value as EffectMode | '')"
              >
                <option value="">跟随模式</option>
                <option value="recorded">用录制结果</option>
                <option value="live">真实执行</option>
                <option value="dry_run">拦截不执行</option>
              </select>
            </div>
          </div>

          <label class="toggle side-effect">
            <input v-model="allowSideEffects" type="checkbox" />
            <span>允许真实执行写操作与对外动作</span>
          </label>
          <p class="warn-note">
            默认关闭。关闭时，任何要求真实执行的写入或对外动作都会被闸门降级为拦截，并留下告警标记。
          </p>

          <button class="primary block" type="button" :disabled="replayBusy" @click="startReplay">
            {{ replayBusy ? '正在提交' : '发起回放' }}
          </button>

          <p v-if="replayError" class="error-note">{{ replayError }}</p>
          <div v-if="replayLaunched" class="launched">
            <p>{{ replayLaunched.plan_summary }}</p>
            <RouterLink :to="{ name: 'run-detail', params: { id: replayLaunched.run_id } }">
              打开回放运行 {{ shortId(replayLaunched.run_id) }}
            </RouterLink>
          </div>
          <p v-if="detail && !detail.agent_registered" class="warn-note">
            这个 Agent 没有注册可重建的实现，服务端无法发起回放。录制与查看不受影响。
          </p>
        </section>

        <section v-if="!isPi" class="panel">
          <h2>沉淀为回归用例</h2>
          <p class="hint">
            把这次失败固定成带断言的用例。断言是确定性的，重跑后会给出明确的通过或失败。
          </p>
          <label class="field">
            <span>用例名称</span>
            <input v-model="caseName" type="text" placeholder="例如：checkout-api 延迟根因定位" />
          </label>
          <label class="field">
            <span>结论应包含</span>
            <input v-model="caseContains" type="text" placeholder="例如：REDIS_POOL_SIZE" />
          </label>
          <button class="ghost block" type="button" :disabled="caseBusy" @click="createCase">
            <PhPlus :size="14" weight="bold" />
            {{ caseBusy ? '正在创建' : '创建用例' }}
          </button>
          <p class="tiny">
            会自动加入「没有错误」与本次调用过的每个工具各一条断言。创建后可在用例页修改。
          </p>
          <p v-if="caseError" class="error-note">{{ caseError }}</p>
          <p v-if="detail.case" class="tiny">
            <PhTestTube :size="12" weight="bold" />
            已关联用例：{{ detail.case.name }}
          </p>
        </section>

        <section v-if="detail.parent || detail.children.length" class="panel">
          <h2>血缘</h2>
          <div v-if="detail.parent" class="lineage-item">
            <span class="label">原始运行</span>
            <RouterLink :to="{ name: 'run-detail', params: { id: detail.parent.id } }" class="mono">
              {{ shortId(detail.parent.id) }}
            </RouterLink>
            <StatusBadge :status="detail.parent.status" />
          </div>
          <div v-for="child in detail.children" :key="child.id" class="lineage-item">
            <span class="label">回放运行</span>
            <RouterLink :to="{ name: 'run-detail', params: { id: child.id } }" class="mono">
              {{ shortId(child.id) }}
            </RouterLink>
            <StatusBadge :status="child.status" />
          </div>
        </section>

        <section class="panel">
          <h2>本次结论</h2>
          <pre class="conclusion">{{ conclusion }}</pre>
        </section>
      </aside>
    </div>
  </template>
</template>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 20px;
  margin-bottom: 16px;
}
.title-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.title-row h1 {
  font-size: var(--step-4);
}
.tailing {
  font-size: 11px;
  color: var(--accent);
}
.meta {
  margin-top: 6px;
  color: var(--text-muted);
  font-size: var(--step-1);
}
.sep {
  margin: 0 8px;
  color: var(--text-faint);
}
.lineage,
.danger-note,
.redaction-note {
  margin-top: 6px;
  font-size: var(--step-1);
}
.lineage {
  color: var(--text-muted);
}
.danger-note {
  color: var(--danger);
}
.redaction-note {
  color: var(--text-faint);
}
.head-actions {
  display: flex;
  gap: 8px;
}

.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 372px;
  gap: 18px;
  margin-top: 18px;
  align-items: start;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 10px;
}
.section-head h2 {
  font-size: var(--step-3);
}
.steps {
  list-style: none;
  margin: 0;
  padding: 0;
}
.side {
  display: grid;
  gap: 14px;
  position: sticky;
  top: 72px;
}
.panel {
  border: 1px solid var(--border);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  padding: 14px 16px;
  display: grid;
  gap: 10px;
}
.panel h2 {
  font-size: var(--step-3);
}
.hint {
  font-size: var(--step-1);
  color: var(--text-muted);
}
.field {
  display: grid;
  gap: 4px;
}
.field > span {
  font-size: 11px;
  color: var(--text-faint);
}
input[type='text'],
select,
textarea {
  width: 100%;
  background: var(--bg-inset);
  color: var(--text);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-control);
  padding: 7px 10px;
  font-family: inherit;
  font-size: var(--step-1);
}
textarea {
  font-family: var(--font-mono);
  resize: vertical;
  line-height: 1.6;
}
.segmented {
  display: grid;
  grid-template-columns: 1fr 1fr;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-control);
  overflow: hidden;
}
.segmented button {
  padding: 7px;
  background: transparent;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
}
.segmented button.active {
  background: var(--accent-dim);
  color: var(--accent-strong);
}
.mode-note {
  font-size: 11px;
  color: var(--text-muted);
}
.disclosure {
  text-align: left;
  background: transparent;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-control);
  color: var(--text-muted);
  padding: 7px 10px;
  cursor: pointer;
  font-size: var(--step-1);
}
.disclosure:hover {
  color: var(--text);
}
.advanced {
  display: grid;
  gap: 6px;
  max-height: 260px;
  overflow-y: auto;
  padding-right: 4px;
}
.step-override {
  display: grid;
  grid-template-columns: 1fr 130px;
  gap: 8px;
  align-items: center;
}
.step-label {
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.step-override select {
  font-size: 11px;
  padding: 4px 6px;
}
.toggle {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--text-muted);
  font-size: var(--step-1);
  cursor: pointer;
}
.side-effect {
  color: var(--warning);
}
.warn-note,
.tiny,
.error-note {
  font-size: 11px;
}
.warn-note {
  color: var(--warning);
}
.tiny {
  color: var(--text-faint);
}
.error-note {
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
}
.block {
  width: 100%;
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
  background: transparent;
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
.launched {
  border: 1px solid var(--accent-ring);
  border-radius: var(--radius-control);
  background: var(--accent-dim);
  padding: 10px;
  font-size: var(--step-1);
  display: grid;
  gap: 5px;
}
.lineage-item {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: var(--step-1);
}
.lineage-item .label {
  color: var(--text-faint);
  min-width: 66px;
}
.conclusion {
  margin: 0;
  max-height: 340px;
  overflow: auto;
  font-family: var(--font-mono);
  font-size: var(--step-1);
  line-height: 1.65;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
@media (max-width: 1100px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }
  .side {
    position: static;
  }
}
</style>
