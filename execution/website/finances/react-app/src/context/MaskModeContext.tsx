import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  useEffect,
  type ReactNode,
} from 'react'
import {
  maskMoney,
  maskCount,
  maskPercent,
  maskNumeric,
  maskLabel,
  maskTaxonomyLabel,
  maskFreeform,
  maskDeepValue,
  newMaskSalt,
} from '@/lib/mask'
import { humanizePropertyKey } from '@/lib/propertyLabels'

const STORAGE_KEY = 'finances-mask-mode'
const SALT_KEY = 'finances-mask-salt'

interface MaskModeContextValue {
  enabled: boolean
  setEnabled: (next: boolean) => void
  /** Rotate salt (new fake values while mask stays on). */
  rerollMask: () => void
  /** Currency / balance–like amounts (plausible magnitude & rounding). */
  maskMoney: (n: number, key?: string) => number
  /** Counts, list lengths, page indices. */
  maskCount: (n: number, key?: string) => number
  /** APR / percent-style values (same scale as input, e.g. 3.25 → ~3.1). */
  maskPercent: (n: number, key?: string) => number
  /** Infers money vs count vs percent from key + value. */
  maskNumber: (n: number, key?: string) => number
  text: (s: string | null | undefined) => string
  /**
   * Entity / timeline type slugs (`financial_account`, `observation_created`).
   * When masked: initials + short id (not fake bank names).
   */
  taxonomyLabel: (slug: string | null | undefined) => string
  freeform: (s: string) => string
  deep: (v: unknown) => unknown
}

const MaskModeContext = createContext<MaskModeContextValue | null>(null)

function readStoredEnabled(): boolean {
  if (typeof window === 'undefined') return false
  return localStorage.getItem(STORAGE_KEY) === '1'
}

function readOrCreateSalt(): string {
  if (typeof window === 'undefined') return 'ssr'
  let s = sessionStorage.getItem(SALT_KEY)
  if (!s) {
    s = newMaskSalt()
    sessionStorage.setItem(SALT_KEY, s)
  }
  return s
}

export function MaskModeProvider({ children }: { children: ReactNode }) {
  const [enabled, setEnabledState] = useState(readStoredEnabled)
  const [salt, setSalt] = useState(readOrCreateSalt)

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, enabled ? '1' : '0')
  }, [enabled])

  const setEnabled = useCallback((next: boolean) => {
    setEnabledState(next)
    if (next) {
      const ns = newMaskSalt()
      sessionStorage.setItem(SALT_KEY, ns)
      setSalt(ns)
    }
  }, [])

  const rerollMask = useCallback(() => {
    const ns = newMaskSalt()
    sessionStorage.setItem(SALT_KEY, ns)
    setSalt(ns)
  }, [])

  const value = useMemo<MaskModeContextValue>(() => {
    return {
      enabled,
      setEnabled,
      rerollMask,
      maskMoney: (n, key = '') => (enabled ? maskMoney(n, salt, key) : n),
      maskCount: (n, key = '') => (enabled ? maskCount(n, salt, key) : n),
      maskPercent: (n, key = '') => (enabled ? maskPercent(n, salt, key) : n),
      maskNumber: (n, key = '') => (enabled ? maskNumeric(n, salt, key) : n),
      text: (s) => {
        if (s == null || s === '') return s ?? ''
        return enabled ? maskLabel(s, salt) : s
      },
      taxonomyLabel: (slug) => {
        if (slug == null || slug === '') return '—'
        const t = slug.trim()
        return enabled ? maskTaxonomyLabel(t, salt) : humanizePropertyKey(t)
      },
      freeform: (s) => (enabled ? maskFreeform(s, salt) : s),
      deep: (v) => (enabled ? maskDeepValue(v, salt) : v),
    }
  }, [enabled, salt, setEnabled, rerollMask])

  return <MaskModeContext.Provider value={value}>{children}</MaskModeContext.Provider>
}

export function useMaskMode(): MaskModeContextValue {
  const ctx = useContext(MaskModeContext)
  if (!ctx) {
    throw new Error('useMaskMode must be used within MaskModeProvider')
  }
  return ctx
}
