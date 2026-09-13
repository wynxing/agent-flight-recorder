<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  PhBrain,
  PhCaretRight,
  PhCheckCircle,
  PhFlag,
  PhStackSimple,
  PhWarningOctagon,
  PhWrench,
} from '@phosphor-icons/vue'
import type { AgentEvent } from '@/api/types'
import SourceBadge from './SourceBadge.vue'
import SideEffectBadge from './SideEffectBadge.vue'
import JsonBlock from './JsonBlock.vue'
import { argsOf, EVENT_LABELS, formatDuration, inlineJson, textOf, toolCallsOf, truncate } from '@/utils/format'

const props = defineProps<{ event: AgentEvent; highlight?: boolean; pinnable?: boolean }>()
defineEmits<{ pick: [event: AgentEvent] }>()

const expanded = ref(false)

const icon = computed(() => {
  switch (props.event.type) {
    case 'model_call':
      return PhBrain
    case 'tool_call':
      return PhWrench
    case 'state_snapshot':
      return PhStackSimple
    case 'error':
      return PhWarningOctagon
    case 'run_finished':
      return PhCheckCircle
    default:
      return PhFlag
  }
})

const isWarning = computed(() => props.event.attributes?.severity === 'warning')
const isSynthetic = computed(() => props.event.attributes?.synthetic === true)
const showDetails = computed(() => props.event.type !== 'state_snapshot')

const summary = computed(() => {
  const event = props.event
  if (event.type === 'tool_call') {
    return truncate(inlineJson(argsOf(event)), 180)
  }
  if (event.type === 'model_call') {
    const calls = toolCallsOf(event)
    const names = calls.map((call) => call.name).filter(Boolean).join(', ')
    const text = textOf(event)
    if (text && names) return truncate(text, 120) + `  →  ${names}`
    if (text) return truncate(text, 160)
    return names ? `→ ${names}` : ''
  }
  if (event.type === 'run_started') {
    const task = event.input?.task
    return typeof task === 'string' ? truncate(task, 200) : ''
  }
  if (event.type === 'run_finished') {
    const result = event.output?.result
    return typeof result === 'string' ? truncate(result, 200) : ''
  }
  if (event.type === 'error') return event.error?.message ?? ''
  return ''
})

const reasonNote = computed(() => {
  const reason = props.event.attributes?.reason
  if (reason === 'side_effect_gate') return '副作用闸门拦截：这一步要求真实执行对外动作，但未获得授权。'
  if (reason === 'no_recording') return '父 Run 中没有匹配的录制结果，平台拒绝编造结果。'
  if (reason === 'before_fork_point') return '位于分叉点之前，按录制结果复现。'
  if (reason === 'side_effect_executed') return '这一步真实执行了副作用，已在时间线上留痕。'
  if (reason === 'beyond_parent_tail') return '父 Run 之后新增的步骤。'
  return ''
})
</script>

<template>
  <li class="step" :class="{ warning: isWarning, highlight }">
    <div class="rail">
      <span class="seq mono">{{ event.seq }}</span>
      <span class="marker" :class="event.type"><component :is="icon" :size="13" weight="bold" /></span>
    </div>

    <div class="content">
      <button class="head" type="button" :disabled="!showDetails" @click="expanded = !expanded">
        <PhCaretRight v-if="showDetails" :size="12" weight="bold" class="caret" :class="{ open: expanded }" />
        <span class="type">{{ EVENT_LABELS[event.type] }}</span>
        <span v-if="event.name" class="name mono">{{ event.name }}</span>
        <SourceBadge :source="event.effect_source" />
        <SideEffectBadge :effect="event.side_effect" />
        <span v-if="isSynthetic" class="synthetic">合成结果</span>
        <span class="spacer" />
        <span v-if="event.tokens?.total" class="meta mono">{{ event.tokens.total }} tok</span>
        <span class="meta mono">{{ formatDuration(event.duration_ms) }}</span>
        <span
          v-if="pinnable && event.type !== 'state_snapshot'"
          class="pin"
          role="button"
          tabindex="0"
          title="以这一步作为回放起点"
          @click.stop="$emit('pick', event)"
          @keydown.enter.stop="$emit('pick', event)"
        >
          从这里回放
        </span>
      </button>

      <p v-if="summary" class="summary" :class="{ mono: event.type === 'tool_call' }">{{ summary }}</p>
      <p v-if="reasonNote" class="reason">{{ reasonNote }}</p>

      <div v-if="expanded && showDetails" class="details">
        <JsonBlock v-if="event.error" :value="event.error" label="错误" />
        <JsonBlock v-if="event.input" :value="event.input" label="输入" />
        <JsonBlock v-if="event.output" :value="event.output" label="输出" />
        <JsonBlock
          v-if="event.attributes && Object.keys(event.attributes).length"
          :value="event.attributes"
          label="属性"
        />
        <p v-if="event.redactions?.length" class="redaction">
          已脱敏：{{ event.redactions.join(', ') }}
        </p>
      </div>
    </div>
  </li>
</template>

<style scoped>
.step {
  display: grid;
  grid-template-columns: 54px 1fr;
  gap: 0;
  position: relative;
}
.step::before {
  content: '';
  position: absolute;
  left: 26px;
  top: 22px;
  bottom: -2px;
  width: 1px;
  background: var(--border);
}
.step:last-child::before {
  display: none;
}
.rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding-top: 8px;
}
.seq {
  font-size: 10.5px;
  color: var(--text-faint);
}
.marker {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--bg-raised);
  border: 1px solid var(--border);
  color: var(--text-muted);
  z-index: 1;
}
.marker.model_call {
  color: var(--accent);
  border-color: rgba(76, 194, 212, 0.3);
}
.marker.tool_call {
  color: var(--live);
  border-color: rgba(89, 201, 138, 0.28);
}
.marker.error {
  color: var(--danger);
  border-color: rgba(224, 112, 123, 0.34);
}
.marker.run_finished {
  color: var(--live);
}

.content {
  border: 1px solid var(--border);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  margin-bottom: 8px;
  overflow: hidden;
}
.step.highlight .content {
  border-color: var(--accent-ring);
}
.step.warning .content {
  border-left: 2px solid var(--warning);
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  background: transparent;
  border: 0;
  cursor: pointer;
  text-align: left;
}
.head:disabled {
  cursor: default;
}
.caret {
  color: var(--text-faint);
  transition: transform 120ms ease;
}
.caret.open {
  transform: rotate(90deg);
}
.type {
  font-size: var(--step-1);
  color: var(--text-muted);
  white-space: nowrap;
}
.name {
  font-size: var(--step-2);
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.spacer {
  flex: 1;
}
.meta {
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
}
.synthetic {
  font-size: 10.5px;
  color: var(--dryrun);
  border: 1px dashed rgba(224, 164, 88, 0.4);
  border-radius: var(--radius-chip);
  padding: 0 6px;
}
.pin {
  font-size: 10.5px;
  color: var(--text-faint);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-chip);
  padding: 0 7px;
  opacity: 0;
  transition: opacity 120ms ease, color 120ms ease;
  white-space: nowrap;
}
.head:hover .pin,
.pin:focus-visible {
  opacity: 1;
}
.pin:hover {
  color: var(--accent-strong);
  border-color: var(--accent-ring);
}
@media (hover: none) {
  .pin {
    opacity: 1;
  }
}
.summary {
  padding: 0 12px 9px 30px;
  color: var(--text-muted);
  font-size: var(--step-1);
  word-break: break-word;
}
.reason {
  padding: 0 12px 9px 30px;
  color: var(--warning);
  font-size: var(--step-1);
}
.details {
  display: grid;
  gap: 8px;
  padding: 0 12px 12px 30px;
}
.redaction {
  font-size: 11px;
  color: var(--text-faint);
}
@media (max-width: 768px) {
  .step {
    grid-template-columns: 40px 1fr;
  }
  .step::before {
    left: 19px;
  }
  .details,
  .summary,
  .reason {
    padding-left: 12px;
  }
  .head {
    flex-wrap: wrap;
  }
}
</style>
