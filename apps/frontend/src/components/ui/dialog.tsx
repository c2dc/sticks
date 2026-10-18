import * as React from "react"
import { X } from "lucide-react"

import { cn } from "@/lib/utils"

/**
 * Minimal accessible modal dialog.
 *
 * This project does not depend on `@radix-ui/react-dialog`, so rather than pull
 * a new dependency this follows the shadcn dialog shape (Dialog / DialogContent
 * / DialogHeader / DialogTitle / DialogDescription / DialogFooter) with a small,
 * self-contained accessible implementation:
 *
 * - `role="dialog"` + `aria-modal` on the panel, wired to its title/description.
 * - Escape closes; clicking the backdrop closes.
 * - Focus moves into the dialog on open and is restored to the trigger on close.
 * - Background scroll is locked while open.
 *
 * Closing is always driven by `onOpenChange(false)` so callers stay in control
 * of side effects (e.g. "cancel does nothing" — Req. 6.7).
 */

interface DialogContextValue {
  onOpenChange: (open: boolean) => void
  titleId: string
  descriptionId: string
}

const DialogContext = React.createContext<DialogContextValue | null>(null)

function useDialogContext(component: string): DialogContextValue {
  const ctx = React.useContext(DialogContext)
  if (!ctx) {
    throw new Error(`${component} must be used within <Dialog>`)
  }
  return ctx
}

export interface DialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  children: React.ReactNode
}

export function Dialog({ open, onOpenChange, children }: DialogProps) {
  const titleId = React.useId()
  const descriptionId = React.useId()

  const contextValue = React.useMemo<DialogContextValue>(
    () => ({ onOpenChange, titleId, descriptionId }),
    [onOpenChange, titleId, descriptionId],
  )

  if (!open) return null

  return (
    <DialogContext.Provider value={contextValue}>
      {children}
    </DialogContext.Provider>
  )
}

export interface DialogContentProps
  extends React.HTMLAttributes<HTMLDivElement> {}

export const DialogContent = React.forwardRef<HTMLDivElement, DialogContentProps>(
  ({ className, children, ...props }, forwardedRef) => {
    const { onOpenChange, titleId, descriptionId } = useDialogContext("DialogContent")
    const panelRef = React.useRef<HTMLDivElement | null>(null)

    const setRefs = React.useCallback(
      (node: HTMLDivElement | null) => {
        panelRef.current = node
        if (typeof forwardedRef === "function") forwardedRef(node)
        else if (forwardedRef) forwardedRef.current = node
      },
      [forwardedRef],
    )

    // Move focus into the dialog on open; restore it to the previously focused
    // element (typically the trigger) on close.
    React.useEffect(() => {
      const previouslyFocused = document.activeElement as HTMLElement | null
      panelRef.current?.focus()

      const previousOverflow = document.body.style.overflow
      document.body.style.overflow = "hidden"

      return () => {
        document.body.style.overflow = previousOverflow
        previouslyFocused?.focus?.()
      }
    }, [])

    // Escape closes the dialog.
    React.useEffect(() => {
      function onKeyDown(event: KeyboardEvent) {
        if (event.key === "Escape") {
          event.stopPropagation()
          onOpenChange(false)
        }
      }
      document.addEventListener("keydown", onKeyDown)
      return () => document.removeEventListener("keydown", onKeyDown)
    }, [onOpenChange])

    return (
      <div
        className="fixed inset-0 z-50 flex items-center justify-center p-4"
        // Backdrop click closes.
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) onOpenChange(false)
        }}
      >
        <div
          className="absolute inset-0 bg-black/60 backdrop-blur-sm"
          aria-hidden="true"
        />
        <div
          ref={setRefs}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          aria-describedby={descriptionId}
          tabIndex={-1}
          className={cn(
            "relative z-10 flex max-h-[85vh] w-full max-w-2xl flex-col gap-4 overflow-hidden rounded-xl border bg-background p-6 shadow-lg outline-none",
            className,
          )}
          {...props}
        >
          {children}
        </div>
      </div>
    )
  },
)
DialogContent.displayName = "DialogContent"

export function DialogHeader({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("flex flex-col gap-1.5 text-left", className)}
      {...props}
    />
  )
}
DialogHeader.displayName = "DialogHeader"

export function DialogFooter({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex flex-col-reverse gap-2 sm:flex-row sm:justify-end",
        className,
      )}
      {...props}
    />
  )
}
DialogFooter.displayName = "DialogFooter"

export const DialogTitle = React.forwardRef<
  HTMLHeadingElement,
  React.HTMLAttributes<HTMLHeadingElement>
>(({ className, ...props }, ref) => {
  const { titleId } = useDialogContext("DialogTitle")
  return (
    <h2
      ref={ref}
      id={titleId}
      className={cn("text-lg font-semibold leading-none tracking-tight", className)}
      {...props}
    />
  )
})
DialogTitle.displayName = "DialogTitle"

export const DialogDescription = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, ...props }, ref) => {
  const { descriptionId } = useDialogContext("DialogDescription")
  return (
    <p
      ref={ref}
      id={descriptionId}
      className={cn("text-sm text-muted-foreground", className)}
      {...props}
    />
  )
})
DialogDescription.displayName = "DialogDescription"

/** Optional top-right close button; delegates to `onOpenChange(false)`. */
export function DialogClose({
  className,
  label,
}: {
  className?: string
  /** Accessible label for the icon-only close button. */
  label: string
}) {
  const { onOpenChange } = useDialogContext("DialogClose")
  return (
    <button
      type="button"
      onClick={() => onOpenChange(false)}
      aria-label={label}
      className={cn(
        "absolute right-4 top-4 rounded-sm opacity-70 ring-offset-background transition-opacity hover:opacity-100 focus:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        className,
      )}
    >
      <X className="size-4" aria-hidden="true" />
    </button>
  )
}
DialogClose.displayName = "DialogClose"
