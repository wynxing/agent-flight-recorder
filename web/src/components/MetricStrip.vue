<script setup lang="ts">
import { computed } from 'vue'
import type { RunSummary } from '@/api/types'
import { formatCost, formatDuration, formatNumber } from '@/utils/format'

const props = defineProps<{ summary: RunSummary }>()

const items = computed(() => {
  const s = props.summary
  return [
    { label: '步骤', value: formatNumber(s.event_count) },
    { label: '模型调用', value: formatNumber(s.model_calls) },
    { label: '工具调用', value: formatNumber(s.tool_calls) },
    { label: '错误', value: formatNumber(s.error_count), tone: s.error_count > 0 ? 'danger' : undefined },
    { label: '输入 token', value: formatNumber(s.tokens_input) },
    { label: '输出 token', value: formatNumber(s.tokens_output) },
    { label: '耗时', value: formatDuration(s.duration_ms) },
    { label: '成本估算', value: formatCost(s.cost_usd) },
  ]
})

const effects = computed(() => Object.entries(props.summary.effect_counts ?? {}))
</script>

<template>
  <div class="strip">
    <div class="grid">
      <div v-for="item in items" :key="item.label" class="cell">
        <span class="label">{{ item.label }}</span>
        <span class="value mono" :class="item.tone">{{ item.value }}</span>
      </div>
    </div>
    <p v-if="effects.length" class="effects">
      本次执行结果来源：
      <span v-for="([key, count], index) in effects" :key="key" class="effect mono">
        {{ key }} ×{{ count }}<span v-if="index < effects.length - 1">，</span>
      </span>
    </p>
    <p class="note">token 与成本为估算值。</p>
  </div>
</template>

<style scoped>
.strip {
  border: 1px solid var(--border);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  padding: 14px 16px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px 20px;
}
.cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.label {
  font-size: 11px;
  color: var(--text-faint);
}
.value {
  font-size: var(--step-3);
  color: var(--text);
}
.value.danger {
  color: var(--danger);
}
.effects {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  font-size: var(--step-1);
  color: var(--text-muted);
}
.effect {
  color: var(--text);
}
.note {
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-faint);
}
@media (max-width: 768px) {
  .grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>

