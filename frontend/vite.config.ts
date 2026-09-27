import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const backend = process.env.BACKEND_URL ?? 'http://localhost:8080'
const proxy = Object.fromEntries(
  ['/api', '/actuator', '/swagger-ui', '/v3'].map((p) => [p, { target: backend, changeOrigin: true }]),
)

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
  worker: { format: 'es' },
  build: { chunkSizeWarningLimit: 1200 },
})
