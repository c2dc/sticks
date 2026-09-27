import type { ReactNode } from "react"

/**
 * Base application layout: a header, a main content area for the pipeline
 * overview, and an aside slot for the preferences panel. Kept minimal; screens
 * are filled in by later tasks.
 */
export function AppLayout({
  children,
  aside,
}: {
  children: ReactNode
  aside?: ReactNode
}) {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <h1 className="text-lg font-semibold">
            Pipeline UI <span className="text-muted-foreground">— sticks</span>
          </h1>
        </div>
      </header>
      <div className="mx-auto grid max-w-6xl gap-6 px-4 py-6 lg:grid-cols-[1fr_18rem]">
        <main className="flex flex-col gap-6">{children}</main>
        {aside ? <aside className="flex flex-col gap-6">{aside}</aside> : null}
      </div>
    </div>
  )
}
