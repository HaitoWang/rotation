import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// 本地默认输出到后端静态目录；容器构建通过 VITE_OUT_DIR 覆盖为 /static。
// dev 模式下把 /api 代理到本地 FastAPI，方便热更新开发。
// 按需引入 Element Plus 组件/指令（importStyle:false → 保留全量 CSS，仅 tree-shake JS）。
export default defineConfig({
  plugins: [
    vue(),
    AutoImport({ resolvers: [ElementPlusResolver({ importStyle: false })] }),
    Components({ resolvers: [ElementPlusResolver({ importStyle: false })] }),
  ],
  // FastAPI 在 /static 挂载静态资源，index.html 在 / 返回，故 base 用 /static/
  base: '/static/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: process.env.VITE_OUT_DIR || '../backend/app/webui/static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5666,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
})
