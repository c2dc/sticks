import type { ReactNode } from "react"

import { IngestionStatusBar } from "@/components/IngestionStatusBar"
import { LlmStatusProvider } from "@/components/llm/LlmStatusProvider"
import { NavBar } from "@/components/NavBar"

/**
 * Base application layout: a top navigation bar (with the STICKS logo and the
 * theme/language selectors) and a main content area for the pipeline overview.
 * The language-model status provider wraps both so the nav icon and the run
 * screen share one state.
 */
export function AppLayout({ children }: { children: ReactNode }) {
  return (
    <LlmStatusProvider>
      <div className="min-h-screen bg-background text-foreground">
        <NavBar />
        <IngestionStatusBar />
        <div className="mx-auto max-w-6xl px-4 py-6">
          <main className="flex flex-col gap-6">{children}</main>
        </div>
      </div>
    </LlmStatusProvider>
  )
}
