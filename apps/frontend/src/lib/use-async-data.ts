import { useCallback, useEffect, useRef, useState } from "react"

interface Settled<T> {
  key: string
  data: T | null
  error: unknown
}

export interface AsyncData<T> {
  /** Result for the current key; `null` while loading, on error or without a key. */
  data: T | null
  /** Rejection reason for the current key, or `null`. */
  error: unknown
  /** True from the moment `key` changes until its request settles. */
  loading: boolean
  /** Refetches the current key without flipping `loading` (data is kept until it settles). */
  reload: () => void
  /** Replaces the data for the current key (e.g. after a polled reread). */
  mutate: (data: T) => void
}

/**
 * Fetches `fetcher()` whenever `key` changes. `loading`, `data` and `error`
 * are derived during render by comparing the settled result's key with the
 * current one, so a key change never shows stale data and state is only set in
 * async callbacks. A response for an outdated key is ignored.
 * A `null` key disables fetching.
 */
export function useAsyncData<T>(key: string | null, fetcher: () => Promise<T>): AsyncData<T> {
  const [settled, setSettled] = useState<Settled<T> | null>(null)
  const [nonce, setNonce] = useState(0)

  const fetcherRef = useRef(fetcher)
  useEffect(() => {
    fetcherRef.current = fetcher
  })

  useEffect(() => {
    if (key === null) return
    let active = true
    fetcherRef.current().then(
      (data) => {
        if (active) setSettled({ key, data, error: null })
      },
      (error: unknown) => {
        if (active) setSettled({ key, data: null, error })
      },
    )
    return () => {
      active = false
    }
  }, [key, nonce])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  const mutate = useCallback(
    (data: T) => {
      if (key !== null) setSettled({ key, data, error: null })
    },
    [key],
  )

  const current = key !== null && settled?.key === key ? settled : null
  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    loading: key !== null && current === null,
    reload,
    mutate,
  }
}
