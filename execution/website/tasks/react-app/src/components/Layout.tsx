import { useState, useEffect, useMemo } from 'react'
import { NavLink, Outlet, useLocation, useSearchParams } from 'react-router-dom'
import {
  ListTodo,
  Sun,
  Moon,
  ChevronLeft,
  ChevronRight,
  type LucideIcon,
  Tag,
} from 'lucide-react'
import { useEntities } from '@shared/hooks/useEntities'
import { snapshotField } from '@shared/lib/formatters'
import { cn } from '@shared/lib/utils'
import { Button, buttonVariants } from '@shared/components/ui/button'
import {
  taskCategoryKey,
  categoryDisplayLabel,
  categoryToSearchParam,
  categoryFromSearchParam,
  UNCATEGORIZED_PARAM,
} from '@/lib/taskCategory'
import type { Entity } from '@shared/types/neotoma'

type NavLinkItem = {
  to: string
  icon: LucideIcon
  label: string
  end?: boolean
}

const SIDEBAR_COLLAPSED_KEY = 'tasks-sidebar-collapsed'

const TASK_QUERY_PARAMS = { entity_type: 'task' as const, include_snapshots: true, limit: 2000 }

function readSidebarCollapsed(): boolean {
  if (typeof window === 'undefined') return false
  return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'
}

function isCompletedStatus(raw: string | null | undefined): boolean {
  if (!raw) return false
  const key = raw.trim().toLowerCase()
  return key === 'completed' || key === 'complete' || key === 'cancelled' || key === 'canceled' || key === 'closed' || key === 'archived'
}

function isOpenTask(e: Entity): boolean {
  return !isCompletedStatus(snapshotField<string>(e.snapshot, 'status'))
}

const NAV_ITEMS: NavLinkItem[] = [
  { to: '/', icon: ListTodo, label: 'All tasks', end: true },
]

export default function Layout() {
  const { pathname } = useLocation()
  const [searchParams] = useSearchParams()
  const [dark, setDark] = useState(() => {
    if (typeof window === 'undefined') return true
    return (
      localStorage.getItem('theme') === 'dark' ||
      (!localStorage.getItem('theme') && window.matchMedia('(prefers-color-scheme: dark)').matches)
    )
  })
  const [collapsed, setCollapsed] = useState(readSidebarCollapsed)

  const tasksQuery = useEntities(TASK_QUERY_PARAMS, true)

  const categoryNav = useMemo(() => {
    const open = (tasksQuery.data?.entities ?? []).filter(isOpenTask)
    const counts = new Map<string, number>()
    for (const e of open) {
      const k = taskCategoryKey(e)
      counts.set(k, (counts.get(k) ?? 0) + 1)
    }
    const entries = [...counts.entries()].sort((a, b) => {
      const la = categoryDisplayLabel(a[0])
      const lb = categoryDisplayLabel(b[0])
      if (a[0] === '') return 1
      if (b[0] === '') return -1
      return la.localeCompare(lb, undefined, { sensitivity: 'base' })
    })
    return { entries, totalOpen: open.length }
  }, [tasksQuery.data?.entities])

  const selectedCategory = categoryFromSearchParam(searchParams.get('category'))

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
          {!collapsed && <span className="text-sm font-semibold tracking-tight pl-2">Tasks</span>}
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

        <nav className="flex-1 overflow-y-auto py-2 px-2 space-y-3 min-h-0">
          <div className="space-y-0.5">
            {NAV_ITEMS.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={{ pathname: '/', search: '' }}
                className={() =>
                  cn(
                    buttonVariants({ variant: 'ghost', size: 'sm' }),
                    'w-full justify-start gap-3 text-sidebar-foreground',
                    collapsed && 'justify-center px-0',
                    selectedCategory === null
                      ? 'bg-sidebar-accent text-sidebar-accent-foreground font-medium'
                      : 'hover:bg-sidebar-accent/50 text-sidebar-foreground/70',
                  )
                }
              >
                <Icon size={18} />
                {!collapsed && <span>{label}</span>}
              </NavLink>
            ))}
          </div>

          {!collapsed && (
            <p className="px-2 pt-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              {tasksQuery.isLoading ? 'Categories…' : 'Open by category'}
            </p>
          )}
          <div className="space-y-0.5">
            {categoryNav.entries.map(([key, count]) => {
              const search = `?category=${categoryToSearchParam(key)}`
              const href = `/${search}`
              const active =
                selectedCategory !== null &&
                (selectedCategory === key || (key === '' && selectedCategory === ''))
              return (
                <NavLink
                  key={key || UNCATEGORIZED_PARAM}
                  to={href}
                  className={cn(
                    buttonVariants({ variant: 'ghost', size: 'sm' }),
                    'w-full justify-start gap-2 text-sidebar-foreground h-auto min-h-9 py-2',
                    collapsed && 'justify-center px-0',
                    active
                      ? 'bg-sidebar-accent text-sidebar-accent-foreground font-medium'
                      : 'hover:bg-sidebar-accent/50 text-sidebar-foreground/70',
                  )}
                  title={categoryDisplayLabel(key)}
                >
                  <Tag size={16} className="shrink-0 opacity-80" />
                  {!collapsed && (
                    <span className="flex-1 text-left truncate text-sm">
                      {categoryDisplayLabel(key)}
                    </span>
                  )}
                  {!collapsed && (
                    <span className="text-xs tabular-nums text-muted-foreground shrink-0">{count}</span>
                  )}
                </NavLink>
              )
            })}
          </div>
        </nav>

        <div className="border-t border-sidebar-border p-2 space-y-2">
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
        <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-4 border-b border-border bg-background px-4 md:px-6">
          <div className="mx-auto flex w-full max-w-7xl items-center">
            <nav className="text-sm text-muted-foreground">
              {pathname === '/' ? (
                <span className="text-foreground font-medium">
                  {selectedCategory === null
                    ? 'Tasks'
                    : categoryDisplayLabel(selectedCategory)}
                </span>
              ) : (
                <>
                  <a href="/" className="hover:text-foreground transition-colors">Tasks</a>
                  <span className="mx-2">/</span>
                  <span className="text-foreground">Detail</span>
                </>
              )}
            </nav>
          </div>
        </header>
        <div className="min-h-[calc(100vh-3.5rem)] min-w-0 max-w-full flex-1 overflow-x-hidden p-4 md:p-6">
          <div className="mx-auto max-w-7xl">
            <Outlet />
          </div>
        </div>
      </main>
    </div>
  )
}
