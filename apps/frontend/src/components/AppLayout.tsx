import type { ReactNode } from "react"

import { NavBar } from "@/components/NavBar"

/**
 * Base application layout: a top navigation bar (with the STICKS logo and the
 * theme/language selectors) and a main content area for the pipeline overview.
 */
export function AppLayout({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <NavBar />
      <div className="mx-auto max-w-6xl px-4 py-6">
        <main className="flex flex-col gap-6">{children}</main>
      </div>
    </div>
  )
}
