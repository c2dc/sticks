import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * Minimal accessible, non-modal popover. Render it inside a `relative` wrapper
 * that also holds the trigger button; the panel is positioned under that wrapper.
 *
 * - `role="dialog"` (not modal, the page stays usable); label it with `aria-label`.
 * - Escape and a pointer press outside the wrapper close it.
 * - Focus moves into the panel on open and returns to the previous element on close.
 *
 * Closing is always driven by `onOpenChange(false)`. The trigger should set
 * `aria-haspopup="dialog"` and `aria-expanded={open}`.
 */
export interface PopoverProps extends React.HTMLAttributes<HTMLDivElement> {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function Popover({ open, onOpenChange, className, children, ...props }: PopoverProps) {
  if (!open) return null
  return (
    <PopoverPanel onOpenChange={onOpenChange} className={className} {...props}>
      {children}
    </PopoverPanel>
  )
}

function PopoverPanel({
  onOpenChange,
  className,
  children,
  ...props
}: Omit<PopoverProps, "open">) {
  const panelRef = React.useRef<HTMLDivElement | null>(null)

  React.useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null
    panelRef.current?.focus()
    return () => previouslyFocused?.focus?.()
  }, [])

  React.useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.stopPropagation()
        onOpenChange(false)
      }
    }
    function onPointerDown(event: MouseEvent) {
      const anchor = panelRef.current?.parentElement
      if (anchor && !anchor.contains(event.target as Node)) onOpenChange(false)
    }
    document.addEventListener("keydown", onKeyDown)
    document.addEventListener("mousedown", onPointerDown)
    return () => {
      document.removeEventListener("keydown", onKeyDown)
      document.removeEventListener("mousedown", onPointerDown)
    }
  }, [onOpenChange])

  return (
    <div
      ref={panelRef}
      role="dialog"
      tabIndex={-1}
      className={cn(
        "absolute right-0 top-full z-50 mt-2 w-80 max-w-[90vw] rounded-lg border bg-background p-4 shadow-lg outline-none",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  )
}
