<script setup lang="ts">
import { computed } from 'vue'
import { PhLockKey, PhPencilSimple, PhBroadcast } from '@phosphor-icons/vue'
import type { SideEffect } from '@/api/types'
import { SIDE_EFFECT_LABELS } from '@/utils/format'

const props = defineProps<{ effect?: SideEffect | null }>()

const icon = computed(() => {
  if (props.effect === 'write') return PhPencilSimple
  if (props.effect === 'external') return PhBroadcast
  return PhLockKey
})
</script>

<template>
  <span v-if="effect && effect !== 'read'" class="effect" :class="effect">
    <component :is="icon" :size="12" weight="bold" />
    {{ SIDE_EFFECT_LABELS[effect] }}
  </span>
</template>

<style scoped>
.effect {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  border-radius: var(--radius-chip);
  font-size: 11px;
  font-weight: 500;
  border: 1px solid transparent;
  white-space: nowrap;
}
.write {
  color: var(--warning);
  background: var(--warning-dim);
  border-color: rgba(224, 164, 88, 0.3);
}
.external {
  color: var(--danger);
  background: var(--danger-dim);
  border-color: rgba(224, 112, 123, 0.3);
}
</style>

