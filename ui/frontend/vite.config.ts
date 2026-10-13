import path from "node:path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    // Standard web port family (80xx), aligned with the container's published
    // host port so the dev server and the compose preview use the same URL.
    port: 8080,
    proxy: {
      // Proxy REST + WebSocket to the FastAPI backend during dev.
      "/api": {
        target: "http://localhost:8010",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://localhost:8010",
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
