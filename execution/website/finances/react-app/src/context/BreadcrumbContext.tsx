import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

type BreadcrumbContextValue = {
  detailLabel: string | null
  setDetailLabel: (label: string | null) => void
}

const BreadcrumbContext = createContext<BreadcrumbContextValue | null>(null)

export function BreadcrumbProvider({ children }: { children: ReactNode }) {
  const [detailLabel, setDetailLabelState] = useState<string | null>(null)
  const setDetailLabel = useCallback((label: string | null) => {
    setDetailLabelState(label)
  }, [])

  const value = useMemo(
    () => ({ detailLabel, setDetailLabel }),
    [detailLabel, setDetailLabel],
  )

  return (
    <BreadcrumbContext.Provider value={value}>{children}</BreadcrumbContext.Provider>
  )
}

export function useBreadcrumbContext(): BreadcrumbContextValue {
  const ctx = useContext(BreadcrumbContext)
  if (!ctx) {
    throw new Error('useBreadcrumbContext must be used within BreadcrumbProvider')
  }
  return ctx
}

/** Detail pages set the last crumb label (e.g. account or filing title). Cleared on unmount. */
export function useDetailBreadcrumbLabel(label: string | null | undefined) {
  const { setDetailLabel } = useBreadcrumbContext()
  useEffect(() => {
    if (label != null && label !== '') {
      setDetailLabel(label)
      return () => setDetailLabel(null)
    }
    setDetailLabel(null)
    return undefined
  }, [label, setDetailLabel])
}
