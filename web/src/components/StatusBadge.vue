<script setup lang="ts">
import { computed } from 'vue'
import { PhCheckCircle, PhCircleNotch, PhProhibit, PhXCircle } from '@phosphor-icons/vue'
import type { RunStatus } from '@/api/types'
import { STATUS_LABELS } from '@/utils/format'

const props = defineProps<{ status: RunStatus }>()

const icon = computed(() => {
  switch (props.status) {
    case 'succeeded':
      return PhCheckCircle
    case 'failed':
      return PhXCircle
    case 'aborted':
      return PhProhibit
    default:
      return PhCircleNotch
  }
})
</script>

<template>
  <span class="status" :class="status">
    <component :is="icon" :size="13" weight="bold" :class="{ spin: status === 'running' }" />
    {{ STATUS_LABELS[status] }}
  </span>
</template>

<style scoped>
.status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
}
.succeeded {
  color: var(--success);
}
.failed {
  color: var(--danger);
}
.aborted {
  color: var(--text-muted);
}
.running {
  color: var(--accent);
}
.spin {
  animation: spin 1.4s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>

