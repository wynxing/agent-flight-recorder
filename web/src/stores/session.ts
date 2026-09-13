import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'
import type { AgentInfo } from '@/api/types'

export const useSessionStore = defineStore('session', () => {
  const agents = ref<AgentInfo[]>([])
  const loaded = ref(false)
  const error = ref('')

  async function loadAgents(force = false) {
    if (loaded.value && !force) return
    try {
      agents.value = await api.agents()
      loaded.value = true
      error.value = ''
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
    }
  }

  return { agents, loaded, error, loadAgents }
})

