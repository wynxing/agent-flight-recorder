<script setup lang="ts">
import { PhArrowClockwise, PhWarningOctagon } from '@phosphor-icons/vue'

// 加载、空、错误三种状态都必须被设计过，不能只做"成功态"。
withDefaults(
  defineProps<{
    variant: 'loading' | 'empty' | 'error'
    title?: string
    detail?: string
  }>(),
  { title: '', detail: '' },
)

defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="panel" :class="variant">
    <div v-if="variant === 'loading'" class="skeleton" aria-label="加载中">
      <span v-for="row in 3" :key="row" class="bar" />
    </div>

    <template v-else-if="variant === 'error'">
      <PhWarningOctagon :size="22" weight="duotone" class="glyph error-glyph" />
      <p class="title">{{ title || '请求失败' }}</p>
      <p class="detail">{{ detail }}</p>
      <button class="retry" type="button" @click="$emit('retry')">
        <PhArrowClockwise :size="14" weight="bold" />
        重试
      </button>
    </template>

    <template v-else>
      <p class="title">{{ title || '还没有数据' }}</p>
      <p class="detail">{{ detail }}</p>
    </template>
  </div>
</template>

<style scoped>
.panel {
  border: 1px solid var(--border);
  border-radius: var(--radius-container);
  background: var(--bg-raised);
  padding: 20px;
  text-align: center;
}
.title {
  font-weight: 600;
  color: var(--text);
}
.detail {
  margin-top: 6px;
  color: var(--text-muted);
  max-width: 60ch;
  margin-inline: auto;
}
.error-glyph {
  color: var(--danger);
}
.glyph {
  margin-bottom: 8px;
}
.retry {
  margin-top: 14px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: var(--radius-control);
  border: 1px solid var(--border-strong);
  background: var(--bg-hover);
  color: var(--text);
  cursor: pointer;
  transition: background 120ms ease;
}
.retry:hover {
  background: var(--border-strong);
}
.retry:active {
  transform: translateY(1px);
}
.skeleton {
  display: grid;
  gap: 10px;
}
.bar {
  display: block;
  height: 14px;
  border-radius: var(--radius-chip);
  background: linear-gradient(90deg, var(--bg-hover), var(--border), var(--bg-hover));
  background-size: 200% 100%;
  animation: shimmer 1.6s ease-in-out infinite;
}
.bar:nth-child(2) {
  width: 72%;
}
.bar:nth-child(3) {
  width: 48%;
}
@keyframes shimmer {
  0% {
    background-position: 200% 0;
  }
  100% {
    background-position: -200% 0;
  }
}
</style>

