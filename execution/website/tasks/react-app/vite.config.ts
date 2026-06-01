import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, __dirname, '')
  const apiTarget = env.VITE_NEOTOMA_API_URL || 'http://localhost:3180'
  const apiToken = env.VITE_NEOTOMA_TOKEN || ''
  const devPort = Number(env.VITE_PORT || 5296)

  return {
    plugins: [react()],
    base: '/',
    root: __dirname,
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
        '@shared': path.resolve(__dirname, '../../shared/src'),
      },
    },
    server: {
      port: Number.isFinite(devPort) ? devPort : 5296,
      strictPort: true,
      proxy: {
        '/neotoma-api': {
          target: apiTarget,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/neotoma-api/, ''),
          configure: apiToken ? (proxy) => {
            proxy.on('proxyReq', (proxyReq) => {
              proxyReq.setHeader('Authorization', `Bearer ${apiToken}`)
            })
          } : undefined,
        },
      },
      watch: {
        ignored: ['**/node_modules/**', '**/dist/**'],
      },
    },
    build: {
      outDir: 'dist',
    },
  }
})
