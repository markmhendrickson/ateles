import type { NavigateFunction } from 'react-router-dom'
import type { KeyboardEvent, MouseEvent } from 'react'

/** Absolute browser URL for an in-app path (app base is `/`). */
export function toAbsoluteAppUrl(path: string): string {
  if (path.startsWith('http://') || path.startsWith('https://')) return path
  return new URL(path, window.location.origin).href
}

/** Middle mouse button opens a new tab with the same SPA route. */
export function handleRowAuxClick(path: string, e: MouseEvent<HTMLTableRowElement>): void {
  if (e.button !== 1) return
  e.preventDefault()
  window.open(toAbsoluteAppUrl(path), '_blank', 'noopener,noreferrer')
}

/**
 * Plain click → client navigate; Cmd/Ctrl/Shift/Alt+click → new tab (browser-native pattern for SPAs).
 */
export function handleRowClickNavigate(navigate: NavigateFunction, path: string, e: MouseEvent<HTMLTableRowElement>): void {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
    e.preventDefault()
    window.open(toAbsoluteAppUrl(path), '_blank', 'noopener,noreferrer')
    return
  }
  navigate(path)
}

/** Enter / Space on focused row; optional Cmd/Ctrl opens new tab. */
export function handleRowKeyNavigate(
  navigate: NavigateFunction,
  path: string,
  e: KeyboardEvent<HTMLTableRowElement>,
): void {
  if (e.key !== 'Enter' && e.key !== ' ') return
  e.preventDefault()
  if (e.metaKey || e.ctrlKey) {
    window.open(toAbsoluteAppUrl(path), '_blank', 'noopener,noreferrer')
    return
  }
  navigate(path)
}
