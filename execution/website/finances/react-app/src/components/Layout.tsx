import { useState, useEffect } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  LayoutDashboard,
  Landmark,
  FileText,
  CreditCard,
  ReceiptText,
  ArrowRightLeft,
  CalendarClock,
  Database,
  Sun,
  Moon,
  ChevronLeft,
  ChevronRight,
  EyeOff,
  Shield,
  DollarSign,
  Euro,
  type LucideIcon,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useFxRate } from '@/context/FxRateContext'
import { useDisplayUnit } from '@/context/DisplayUnitContext'
import { useMaskMode } from '@/context/MaskModeContext'
import { Button, buttonVariants } from '@/components/ui/button'
import { BreadcrumbProvider } from '@/context/BreadcrumbContext'
import PageBreadcrumbs from '@/components/PageBreadcrumbs'

type NavLinkItem = {
  to: string
  icon: LucideIcon
  label: string
  end?: boolean
  /** Highlight when pathname starts with this (e.g. `/filings` for nested routes). */
  activePathPrefix?: string
}

const SIDEBAR_COLLAPSED_KEY = 'finances-sidebar-collapsed'

function readSidebarCollapsed(): boolean {
  if (typeof window === 'undefined') return false
  return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'
}

const NAV_ITEMS: NavLinkItem[] = [
  { to: '/', icon: LayoutDashboard, label: 'Overview', end: true },
  { to: '/accounts', icon: Landmark, label: 'Accounts' },
  {
    to: '/filings',
    icon: FileText,
    label: 'Filings',
    activePathPrefix: '/filings',
  },
  { to: '/loans', icon: CreditCard, label: 'Loans' },
  { to: '/recurring-expenses', icon: ReceiptText, label: 'Recurring expenses' },
  { to: '/transactions', icon: ArrowRightLeft, label: 'Transactions' },
  { to: '/timeline', icon: CalendarClock, label: 'Timeline' },
  { to: '/explorer', icon: Database, label: 'Entity explorer' },
]

export default function Layout() {
  const { displayUnit, setDisplayUnit } = useDisplayUnit()
  const { enabled: maskOn, setEnabled: setMaskOn } = useMaskMode()
  const { usdPerEur, rateDate, isError } = useFxRate()
  const { pathname } = useLocation()
  const [dark, setDark] = useState(() => {
    if (typeof window === 'undefined') return true
    return (
      localStorage.getItem('theme') === 'dark' ||
      (!localStorage.getItem('theme') && window.matchMedia('(prefers-color-scheme: dark)').matches)
    )
  })
  const [collapsed, setCollapsed] = useState(readSidebarCollapsed)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('theme', dark ? 'dark' : 'light')
  }, [dark])

  useEffect(() => {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0')
  }, [collapsed])

  return (
    <div className="flex h-screen overflow-hidden">
      <aside
        className={cn(
          'flex flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-all duration-200',
          collapsed ? 'w-16' : 'w-56',
        )}
      >
        <div className="flex items-center justify-between px-2 py-3 border-b border-sidebar-border">
          {!collapsed && <span className="text-sm font-semibold tracking-tight pl-2">Finances</span>}
          <Button
            variant="ghost"
            size="icon"
            className="shrink-0 text-sidebar-foreground hover:bg-sidebar-accent"
            onClick={() => setCollapsed(!collapsed)}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
          </Button>
        </div>

        <nav className="flex-1 overflow-y-auto py-2 px-2 space-y-0.5">
          {NAV_ITEMS.map(({ to, icon: Icon, label, end, activePathPrefix }) => (
            <NavLink
              key={to}
              to={to}
              end={end ?? false}
              className={({ isActive }) => {
                const active = activePathPrefix ? pathname.startsWith(activePathPrefix) : isActive
                return cn(
                  buttonVariants({ variant: 'ghost', size: 'sm' }),
                  'w-full justify-start gap-3 text-sidebar-foreground',
                  collapsed && 'justify-center px-0',
                  active
                    ? 'bg-sidebar-accent text-sidebar-accent-foreground font-medium'
                    : 'hover:bg-sidebar-accent/50 text-sidebar-foreground/70',
                )
              }}
            >
              <Icon size={18} />
              {!collapsed && <span>{label}</span>}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-sidebar-border p-2 space-y-2">
          {collapsed ? (
            <div className="flex flex-col gap-1">
              <Button
                type="button"
                variant={displayUnit === 'usd' ? 'secondary' : 'ghost'}
                size="sm"
                className="w-full justify-center px-0 text-sidebar-foreground"
                onClick={() => setDisplayUnit('usd')}
                title="Display amounts in USD (with stored unit below)"
                aria-label="Display USD"
              >
                <DollarSign size={16} />
              </Button>
              <Button
                type="button"
                variant={displayUnit === 'eur' ? 'secondary' : 'ghost'}
                size="sm"
                className="w-full justify-center px-0 text-sidebar-foreground"
                onClick={() => setDisplayUnit('eur')}
                title="Display amounts in EUR (with stored unit below)"
                aria-label="Display EUR"
              >
                <Euro size={16} />
              </Button>
            </div>
          ) : (
            <div className="space-y-1.5 px-0.5">
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground px-1">Display unit</p>
              <div className="flex rounded-md border border-sidebar-border overflow-hidden">
                <Button
                  type="button"
                  variant={displayUnit === 'usd' ? 'secondary' : 'ghost'}
                  size="sm"
                  className={cn(
                    'flex-1 rounded-none h-8 gap-1 text-xs text-sidebar-foreground',
                    displayUnit !== 'usd' && 'hover:bg-sidebar-accent/50',
                  )}
                  onClick={() => setDisplayUnit('usd')}
                  title="Primary amounts in USD; subtitle shows stored / other unit"
                >
                  <DollarSign size={14} />
                  USD
                </Button>
                <Button
                  type="button"
                  variant={displayUnit === 'eur' ? 'secondary' : 'ghost'}
                  size="sm"
                  className={cn(
                    'flex-1 rounded-none h-8 gap-1 text-xs text-sidebar-foreground border-l border-sidebar-border',
                    displayUnit !== 'eur' && 'hover:bg-sidebar-accent/50',
                  )}
                  onClick={() => setDisplayUnit('eur')}
                  title="Primary amounts in EUR; subtitle shows stored / other unit"
                >
                  <Euro size={14} />
                  EUR
                </Button>
              </div>
            </div>
          )}
          <Button
            type="button"
            variant={maskOn ? 'secondary' : 'ghost'}
            size="sm"
            className={cn(
              'w-full justify-start gap-2 text-sidebar-foreground',
              collapsed && 'justify-center px-0',
              !maskOn && 'hover:bg-sidebar-accent/50',
            )}
            onClick={() => setMaskOn(!maskOn)}
            title="Randomize amounts and names client-side for screen sharing"
          >
            {maskOn ? <EyeOff size={16} /> : <Shield size={16} />}
            {!collapsed && <span>{maskOn ? 'Mask on' : 'Mask mode'}</span>}
          </Button>
          {maskOn && !collapsed && (
            <p className="text-[10px] leading-snug text-muted-foreground px-2 pb-1">
              Amounts and labels are randomized for privacy. URLs may still contain real identifiers.
            </p>
          )}
          {!maskOn && !collapsed && (
            <p className="text-[10px] leading-snug text-muted-foreground px-2 pb-1" title="ECB reference rates via Frankfurter">
              EUR/USD: {isError ? '~1.08 (fallback)' : `${usdPerEur.toFixed(4)}`}
              {!isError && rateDate ? ` · ${rateDate}` : ''}
            </p>
          )}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className={cn(
              'w-full justify-start gap-2 text-sidebar-foreground hover:bg-sidebar-accent/50',
              collapsed && 'justify-center px-0',
            )}
            onClick={() => setDark(!dark)}
          >
            {dark ? <Sun size={16} /> : <Moon size={16} />}
            {!collapsed && <span>{dark ? 'Light mode' : 'Dark mode'}</span>}
          </Button>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col overflow-y-auto">
        <BreadcrumbProvider>
          {/* Match shared site Layout PageHeader: sticky bar + border-b */}
          <header className="sticky top-0 z-30 flex h-16 shrink-0 items-center gap-4 border-b border-border bg-background px-4 min-w-0 max-w-full overflow-x-hidden md:px-6">
            <div className="mx-auto flex w-full max-w-7xl items-center">
              <PageBreadcrumbs />
            </div>
          </header>
          {/* Match shared site content shell: p-4 md:p-6 + min-height below header */}
          <div className="min-h-[calc(100vh-4rem)] min-w-0 max-w-full flex-1 overflow-x-hidden p-4 md:p-6">
            <div className="mx-auto max-w-7xl">
              <Outlet />
            </div>
          </div>
        </BreadcrumbProvider>
      </main>
    </div>
  )
}
