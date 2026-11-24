import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Local deployment entry: http://127.0.0.1:8085
// The Python API stays loopback-only on 8106 and is reached through /api.
// Its origin whitelist predates the local frontend port, so the proxy normalises Origin.
const apiProxy = {
  '/api': {
    target: 'http://127.0.0.1:8106',
    changeOrigin: true,
    configure: (proxy: { on: (event: string, callback: (proxyReq: { setHeader: (name: string, value: string) => void }) => void) => void }) => {
      proxy.on('proxyReq', (proxyReq) => {
        proxyReq.setHeader('Origin', 'http://127.0.0.1:8106')
      })
    },
  },
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 8085,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: {
    host: '0.0.0.0',
    port: 8085,
    strictPort: true,
    proxy: apiProxy,
  },
})
