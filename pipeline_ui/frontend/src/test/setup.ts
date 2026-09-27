import "@testing-library/jest-dom/vitest"

import { afterEach } from "vitest"
import { cleanup } from "@testing-library/react"

// Ensure the i18n instance is initialized once for every test (pt-BR default),
// so components using `useTranslation` resolve real strings.
import "@/i18n"

// Unmount rendered trees between tests to keep the jsdom DOM clean.
afterEach(() => {
  cleanup()
})
