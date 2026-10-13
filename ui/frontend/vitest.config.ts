import path from "node:path"
import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"

// Dedicated Vitest config for frontend component/snapshot tests (Task 12.5).
// Kept separate from vite.config.ts so the production build (`tsc -b && vite
// build`) is untouched. Uses the jsdom environment and a setup file that wires
// up @testing-library/jest-dom matchers.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
})
