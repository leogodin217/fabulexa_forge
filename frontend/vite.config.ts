import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    // Used only in VITE_API_MODE=http: forward the control API to the backend
    // FabulMixer driver. Default mode is `mock` and needs no backend.
    proxy: {
      '/api': {
        // `fabexport mixer` defaults to --port 8765 (cli.py). Match it so a
        // default backend launch works with no extra flags.
        target: 'http://localhost:8765',
        changeOrigin: true,
      },
    },
  },
})
