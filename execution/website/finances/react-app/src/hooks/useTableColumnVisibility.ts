import { useCallback, useEffect, useMemo, useState } from 'react'

const LS_PREFIX = 'finances-table-cols:'

export type ColumnToggleDef = { key: string; label: string }

function parseCommaCsv(csv: string): string[] {
  return csv
    .split(',')
    .map(s => s.trim())
    .filter(Boolean)
}

function defaultsRecord(
  columns: ColumnToggleDef[],
  defaultHidden: readonly string[],
  ensureVisible: readonly string[],
): Record<string, boolean> {
  const out: Record<string, boolean> = {}
  for (const c of columns) {
    out[c.key] = !defaultHidden.includes(c.key)
  }
  for (const k of ensureVisible) {
    if (k in out) out[k] = true
  }
  return out
}

function allVisibleRecord(columns: ColumnToggleDef[]): Record<string, boolean> {
  const out: Record<string, boolean> = {}
  for (const c of columns) {
    out[c.key] = true
  }
  return out
}

function loadMerged(
  storageKey: string,
  defaults: Record<string, boolean>,
  ensureVisible: readonly string[],
): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(LS_PREFIX + storageKey)
    if (!raw) return { ...defaults }
    const parsed = JSON.parse(raw) as Record<string, unknown>
    const out = { ...defaults }
    for (const key of Object.keys(defaults)) {
      if (key in parsed && typeof parsed[key] === 'boolean') {
        out[key] = parsed[key] as boolean
      }
    }
    for (const k of ensureVisible) {
      if (k in out) out[k] = true
    }
    return out
  } catch {
    return { ...defaults }
  }
}

function persist(storageKey: string, visible: Record<string, boolean>) {
  try {
    localStorage.setItem(LS_PREFIX + storageKey, JSON.stringify(visible))
  } catch {
    /* ignore quota */
  }
}

/**
 * Per-table column visibility with localStorage when `storageKey` is set.
 * If `storageKey` is omitted/empty, all columns stay visible and toggles are no-ops.
 *
 * @param ensureVisibleKeysCsv — Column keys that stay **on** (merged from localStorage) and cannot be toggled off (e.g. `account_name` on Modelo 720).
 */
export function useTableColumnVisibility(
  storageKey: string | undefined | null,
  columnDefs: ColumnToggleDef[],
  /** Comma-separated column keys to hide by default (only when persisting) */
  defaultHiddenCsv = '',
  ensureVisibleKeysCsv = '',
) {
  const enabled = Boolean(storageKey?.trim())
  const key = storageKey?.trim() || ''

  const defaultHiddenList = useMemo(
    () => (enabled ? parseCommaCsv(defaultHiddenCsv) : []),
    [enabled, defaultHiddenCsv],
  )

  const ensureVisibleList = useMemo(
    () => (enabled ? parseCommaCsv(ensureVisibleKeysCsv) : []),
    [enabled, ensureVisibleKeysCsv],
  )

  const ensureSet = useMemo(() => new Set(ensureVisibleList), [ensureVisibleList])

  const defaults = useMemo(() => {
    if (!enabled) return allVisibleRecord(columnDefs)
    return defaultsRecord(columnDefs, defaultHiddenList, ensureVisibleList)
  }, [enabled, columnDefs, defaultHiddenList, ensureVisibleList])

  const columnKeysSig = columnDefs.map(c => c.key).join('|')

  const [visible, setVisible] = useState<Record<string, boolean>>(() => {
    if (!enabled) return allVisibleRecord(columnDefs)
    const dh = parseCommaCsv(defaultHiddenCsv)
    const ev = parseCommaCsv(ensureVisibleKeysCsv)
    const def = defaultsRecord(columnDefs, dh, ev)
    return loadMerged(key, def, ev)
  })

  useEffect(() => {
    if (!enabled) {
      setVisible(allVisibleRecord(columnDefs))
      return
    }
    const d = defaultsRecord(columnDefs, defaultHiddenList, ensureVisibleList)
    setVisible(prev => {
      const next = { ...d }
      for (const k of Object.keys(d)) {
        if (k in prev) next[k] = prev[k]!
      }
      for (const k of ensureVisibleList) {
        if (k in next) next[k] = true
      }
      return next
    })
  }, [enabled, key, columnKeysSig, columnDefs, defaultHiddenList, ensureVisibleList])

  const toggle = useCallback(
    (colKey: string) => {
      if (!enabled) return
      setVisible(prev => {
        if (!(colKey in defaults)) return prev
        const nextVal = !prev[colKey]
        if (ensureSet.has(colKey) && nextVal === false) return prev
        const next = { ...prev, [colKey]: nextVal }
        const visibleCount = Object.keys(defaults).filter(k => next[k]).length
        if (visibleCount < 1) return prev
        persist(key, next)
        return next
      })
    },
    [enabled, key, defaults, ensureSet],
  )

  const isVisible = useCallback((colKey: string) => visible[colKey] !== false, [visible])

  return { visible, toggle, isVisible, columnDefs, enabled }
}
