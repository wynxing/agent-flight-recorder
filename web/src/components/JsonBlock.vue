<script setup lang="ts">
import { computed, ref } from 'vue'
import { PhCheck, PhCopy } from '@phosphor-icons/vue'
import { prettyJson } from '@/utils/format'

const props = withDefaults(defineProps<{ value: unknown; label?: string; collapsedHeight?: number }>(), {
  label: '',
  collapsedHeight: 260,
})

const copied = ref(false)
const expanded = ref(false)
const text = computed(() => prettyJson(props.value))
const isLong = computed(() => text.value.split('\n').length > 14)

async function copy() {
  try {
    await navigator.clipboard.writeText(text.value)
    copied.value = true
    setTimeout(() => (copied.value = false), 1400)
  } catch {
    copied.value = false
  }
}
</script>

<template>
  <div v-if="text" class="block">
    <div class="head">
      <span v-if="label" class="label">{{ label }}</span>
      <button class="copy" type="button" @click="copy">
        <component :is="copied ? PhCheck : PhCopy" :size="12" weight="bold" />
        {{ copied ? '已复制' : '复制' }}
      </button>
    </div>
    <pre
      class="body"
      :style="isLong && !expanded ? { maxHeight: `${collapsedHeight}px` } : undefined"
      >{{ text }}</pre
    >
    <button v-if="isLong" class="toggle" type="button" @click="expanded = !expanded">
      {{ expanded ? '收起' : '展开全部' }}
    </button>
  </div>
</template>

<style scoped>
.block {
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  background: var(--bg-inset);
  overflow: hidden;
}
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-raised);
}
.label {
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 0.01em;
}
.copy {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid var(--border-strong);
  background: transparent;
  color: var(--text-muted);
  border-radius: var(--radius-chip);
  padding: 2px 8px;
  font-size: 11px;
  cursor: pointer;
}
.copy:hover {
  color: var(--text);
  border-color: var(--accent-ring);
}
.body {
  margin: 0;
  padding: 10px 12px;
  font-size: var(--step-1);
  line-height: 1.6;
  color: var(--text);
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
.toggle {
  width: 100%;
  border: 0;
  border-top: 1px solid var(--border);
  background: var(--bg-raised);
  color: var(--text-muted);
  font-size: 11px;
  padding: 5px;
  cursor: pointer;
}
.toggle:hover {
  color: var(--accent);
}
</style>

