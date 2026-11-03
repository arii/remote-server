import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const apiTarget = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000'
// Absolute asset base: '/' in dev, '/buttons/' in the image build
// (Dockerfile sets VITE_BASE). Relative './' breaks on slashless
// page URLs, where the browser resolves assets to / instead.
const base = process.env.VITE_BASE ?? '/'

export default defineConfig({
  plugins: [react()],
  base,
  server: {
    host: '0.0.0.0',
    port: 5173,
    hmr: true,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})
