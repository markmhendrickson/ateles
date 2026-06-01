import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

export type DisplayCurrency = 'usd' | 'eur'

const STORAGE_KEY = 'finances-display-unit'

function readStored(): DisplayCurrency {
  if (typeof window === 'undefined') return 'usd'
  const v = localStorage.getItem(STORAGE_KEY)
  return v === 'eur' ? 'eur' : 'usd'
}

interface DisplayUnitContextValue {
  displayUnit: DisplayCurrency
  setDisplayUnit: (u: DisplayCurrency) => void
  toggleDisplayUnit: () => void
}

const DisplayUnitContext = createContext<DisplayUnitContextValue | null>(null)

export function DisplayUnitProvider({ children }: { children: ReactNode }) {
  const [displayUnit, setDisplayUnitState] = useState<DisplayCurrency>(readStored)

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, displayUnit)
  }, [displayUnit])

  const setDisplayUnit = useCallback((u: DisplayCurrency) => {
    setDisplayUnitState(u)
  }, [])

  const toggleDisplayUnit = useCallback(() => {
    setDisplayUnitState(u => (u === 'usd' ? 'eur' : 'usd'))
  }, [])

  const value = useMemo(
    () => ({ displayUnit, setDisplayUnit, toggleDisplayUnit }),
    [displayUnit, setDisplayUnit, toggleDisplayUnit],
  )

  return <DisplayUnitContext.Provider value={value}>{children}</DisplayUnitContext.Provider>
}

export function useDisplayUnit(): DisplayUnitContextValue {
  const ctx = useContext(DisplayUnitContext)
  if (!ctx) {
    throw new Error('useDisplayUnit must be used within DisplayUnitProvider')
  }
  return ctx
}
