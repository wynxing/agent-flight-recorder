<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { PhArrowsLeftRight, PhListBullets, PhTestTube, PhVinylRecord } from '@phosphor-icons/vue'
import { api } from '@/api/client'

const health = ref<'checking' | 'ok' | 'down'>('checking')
const dbPath = ref('')

const links = [
  { to: '/runs', label: '运行记录', icon: PhListBullets },
  { to: '/diff', label: '运行对比', icon: PhArrowsLeftRight },
  { to: '/cases', label: '回归用例', icon: PhTestTube },
]

onMounted(async () => {
  try {
    const result = await api.health()
    health.value = result.ok ? 'ok' : 'down'
    dbPath.value = result.db
  } catch {
    health.value = 'down'
  }
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <RouterLink to="/runs" class="brand">
        <PhVinylRecord :size="20" weight="duotone" class="brand-mark" />
        <span class="brand-name">Agent Flight Recorder</span>
      </RouterLink>

      <nav class="nav">
        <RouterLink v-for="link in links" :key="link.to" :to="link.to" class="nav-link">
          <component :is="link.icon" :size="15" weight="bold" />
          {{ link.label }}
        </RouterLink>
      </nav>

      <div class="health" :title="dbPath">
        <span class="dot" :class="health" />
        <span class="health-text">
          {{ health === 'ok' ? '服务端已连接' : health === 'down' ? '服务端未连接' : '正在检查' }}
        </span>
      </div>
    </header>

    <main class="main">
      <slot />
    </main>
  </div>
</template>

<style scoped>
.shell {
  min-height: 100%;
  display: flex;
  flex-direction: column;
}
.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 24px;
  height: 56px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border);
  background: rgba(14, 17, 22, 0.92);
  backdrop-filter: blur(8px);
}
.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  color: var(--text);
}
.brand:hover {
  color: var(--text);
}
.brand-mark {
  color: var(--accent);
}
.brand-name {
  font-weight: 600;
  letter-spacing: -0.012em;
  white-space: nowrap;
}
.nav {
  display: flex;
  align-items: center;
  gap: 4px;
}
.nav-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 11px;
  border-radius: var(--radius-control);
  color: var(--text-muted);
  font-size: var(--step-1);
  white-space: nowrap;
  transition: background 120ms ease, color 120ms ease;
}
.nav-link:hover {
  background: var(--bg-hover);
  color: var(--text);
}
.nav-link.router-link-active {
  background: var(--accent-dim);
  color: var(--accent-strong);
}
.health {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: var(--step-1);
  color: var(--text-muted);
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-faint);
}
.dot.ok {
  background: var(--success);
}
.dot.down {
  background: var(--danger);
}
.main {
  flex: 1;
  width: 100%;
  max-width: 1400px;
  margin: 0 auto;
  padding: 22px 20px 64px;
}
@media (max-width: 900px) {
  .topbar {
    gap: 12px;
    padding: 0 12px;
  }
  .brand-name,
  .health-text {
    display: none;
  }
  .nav-link {
    padding: 6px 8px;
  }
  .main {
    padding: 16px 12px 48px;
  }
}
</style>

