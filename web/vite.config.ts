import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 开发时把 /v1 代理到本地服务端：前端不需要处理 CORS，构建产物由服务端同源托管。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    // 固定绑定 IPv4，避免 Node 只监听 ::1 时脚本里打印的 127.0.0.1 地址连不上。
    host: '127.0.0.1',
    port: 5273,
    proxy: {
      '/v1': {
        target: 'http://127.0.0.1:7710',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})

