<script setup lang="ts">
import { computed } from 'vue'
import type { EffectSource } from '@/api/types'
import { EFFECT_SOURCE_LABELS } from '@/utils/format'

const props = defineProps<{ source?: EffectSource | null }>()

// 来源标记是语义状态，不是装饰：一次回放里有多少步真的跑过，必须一眼可辨。
const tone = computed(() => {
  switch (props.source) {
    case 'live':
      return 'live'
    case 'recorded':
      return 'recorded'
    case 'dry_run':
      return 'dryrun'
    case 'blocked':
      return 'blocked'
    default:
      return 'muted'
  }
})
</script>

<template>
  <span v-if="source" class="badge" :class="tone">{{ EFFECT_SOURCE_LABELS[source] }}</span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  padding: 1px 7px;
  border-radius: var(--radius-chip);
  font-size: 11px;
  font-weight: 500;
  line-height: 1.7;
  white-space: nowrap;
  border: 1px solid transparent;
}
.live {
  color: var(--live);
  background: var(--live-dim);
  border-color: rgba(89, 201, 138, 0.28);
}
.recorded {
  color: var(--recorded);
  background: var(--recorded-dim);
  border-color: rgba(139, 149, 168, 0.24);
}
.dryrun {
  color: var(--dryrun);
  background: var(--dryrun-dim);
  border-color: rgba(224, 164, 88, 0.3);
}
.blocked {
  color: var(--blocked);
  background: var(--blocked-dim);
  border-color: rgba(224, 112, 123, 0.3);
}
.muted {
  color: var(--text-faint);
  background: var(--bg-hover);
}
</style>

