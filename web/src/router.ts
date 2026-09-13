import { createRouter, createWebHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/runs' },
    { path: '/runs', name: 'runs', component: () => import('@/views/RunsView.vue') },
    { path: '/runs/:id', name: 'run-detail', component: () => import('@/views/RunDetailView.vue') },
    { path: '/diff', name: 'diff', component: () => import('@/views/DiffView.vue') },
    { path: '/cases', name: 'cases', component: () => import('@/views/CasesView.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/runs' },
  ],
})

