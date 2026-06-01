import { Fragment } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useBreadcrumbContext } from '@/context/BreadcrumbContext'
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb'

const SEGMENT_LABELS: Record<string, string> = {
  accounts: 'Accounts',
  filings: 'Filings',
  loans: 'Loans',
  'recurring-expenses': 'Recurring expenses',
  transactions: 'Transactions',
  timeline: 'Timeline',
  explorer: 'Entity explorer',
  'modelo-720': 'Modelo 720',
  'modelo-721': 'Modelo 721',
}

function titleCaseSegment(segment: string): string {
  if (/^\d{4}$/.test(segment)) return segment
  return segment
    .split('-')
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}

function isLikelyEntityId(segment: string): boolean {
  return /^ent_[a-z0-9]+$/i.test(segment)
}

function fallbackForEntitySegment(parentSegment: string | undefined): string {
  if (parentSegment === 'accounts') return 'Account'
  if (parentSegment === 'filings') return 'Filing'
  return 'Details'
}

export type BreadcrumbCrumb = {
  label: string
  href?: string
}

export function buildBreadcrumbsFromPathname(
  pathname: string,
  detailLabel: string | null,
): BreadcrumbCrumb[] {
  const segments = pathname.split('/').filter(Boolean)
  const crumbs: BreadcrumbCrumb[] = []

  if (segments.length === 0) {
    crumbs.push({ label: 'Overview' })
    return crumbs
  }

  crumbs.push({ label: 'Overview', href: '/' })

  let path = ''
  segments.forEach((segment, index) => {
    path += `/${segment}`
    const isLast = index === segments.length - 1
    const parent = index > 0 ? segments[index - 1] : undefined

    let label: string
    if (isLast && detailLabel) {
      label = detailLabel
    } else if (isLikelyEntityId(segment)) {
      label = detailLabel ?? fallbackForEntitySegment(parent)
    } else {
      label = SEGMENT_LABELS[segment] ?? titleCaseSegment(segment)
    }

    crumbs.push({
      label,
      href: isLast ? undefined : path,
    })
  })

  return crumbs
}

export default function PageBreadcrumbs() {
  const location = useLocation()
  const { detailLabel } = useBreadcrumbContext()
  const crumbs = buildBreadcrumbsFromPathname(location.pathname, detailLabel)

  return (
    <Breadcrumb className="min-w-0 flex-1 overflow-hidden max-w-full">
      <BreadcrumbList className="flex-nowrap min-w-0 max-w-full">
        {crumbs.map((crumb, index) => (
          <Fragment key={`${crumb.label}-${index}`}>
            <BreadcrumbItem
              className={cn(
                'min-w-0',
                index === crumbs.length - 1 ? 'flex-1 min-w-0' : 'shrink-0',
              )}
            >
              {crumb.href == null ? (
                <BreadcrumbPage className="truncate block w-full max-w-[min(100%,28rem)]">
                  {crumb.label}
                </BreadcrumbPage>
              ) : (
                <BreadcrumbLink asChild>
                  <Link to={crumb.href} className="truncate whitespace-nowrap">
                    {crumb.label}
                  </Link>
                </BreadcrumbLink>
              )}
            </BreadcrumbItem>
            {index < crumbs.length - 1 && <BreadcrumbSeparator className="shrink-0" />}
          </Fragment>
        ))}
      </BreadcrumbList>
    </Breadcrumb>
  )
}
