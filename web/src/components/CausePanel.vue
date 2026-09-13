<script setup lang="ts">
import { computed } from 'vue'
import { PhGauge, PhInfo, PhProhibit, PhWarningDiamond } from '@phosphor-icons/vue'
import { causeInfo, type InconclusiveCause } from '@/utils/format'

// 一个成因在运行详情页与用例页必须是同一句话、同一个颜色，因此渲染只在这里实现一次。
const props = defineProps<{ cause?: InconclusiveCause | null; title?: string; compact?: boolean }>()

const resolved = computed(() => props.cause ?? null)
const info = computed(() => causeInfo(resolved.value?.code))
const icon = computed(() => {
  // 预算触顶与「副作用被拦截」同属「这次执行没有跑完」：同色不同图标，靠标签区分。
  if (info.value.tone === 'budget') return PhGauge
  if (info.value.tone === 'blocked') return PhProhibit
  if (info.value.tone === 'unknown') return PhInfo
  return PhWarningDiamond
})

</script>

<template>
  <div v-if="resolved" class="cause" :class="[info.tone, { compact }]">
    <p class="head">
      <component :is="icon" :size="14" weight="bold" />
      <span class="label">{{ title ? `${title}：` : '' }}{{ info.label }}</span>
    </p>
    <p class="action">{{ info.action }}</p>
    <details v-if="resolved.detail" class="detail">
      <summary>原始说明（{{ resolved.code }}）</summary>
      <pre>{{ resolved.detail }}</pre>
    </details>
  </div>
</template>

<style scoped>
.cause {
  border: 1px solid var(--border-strong);
  border-left-width: 3px;
  border-radius: var(--radius-control);
  padding: 9px 11px;
  display: grid;
  gap: 5px;
  font-size: var(--step-1);
}
/* 视觉分档与「未通过」明确分开：录制质量用琥珀，副作用拦截用中性青灰，未知用灰。 */
.cause.recording {
  border-left-color: var(--warning);
  color: var(--warning);
}
.cause.blocked {
  border-left-color: var(--accent);
  color: var(--accent-strong);
}
.cause.budget {
  border-left-color: var(--accent);
  color: var(--accent-strong);
}
.cause.unknown {
  border-left-color: var(--text-faint);
  color: var(--text-muted);
}
.head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
}
.action {
  color: var(--text);
  line-height: 1.6;
}
.compact .action {
  font-size: 11px;
}
.detail summary {
  cursor: pointer;
  color: var(--text-faint);
  font-size: 11px;
}
.detail pre {
  margin: 5px 0 0;
  font-family: var(--font-mono);
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-muted);
}
</style>
