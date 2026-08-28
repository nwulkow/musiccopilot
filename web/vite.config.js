import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// The dev server proxies the API to the Python process, so `npm run dev` and
// the built app (served by FastAPI from web/dist) hit identical URLs and no
// base-URL switching is needed in the client. start.sh exports the port it
// gave the Python process, so the proxy follows a `--port` there.
const api = process.env.SCRIPTUM_API_PORT || '9000'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: `http://127.0.0.1:${api}`, changeOrigin: true },
      '/ws': { target: `ws://127.0.0.1:${api}`, ws: true },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 900 },
})
