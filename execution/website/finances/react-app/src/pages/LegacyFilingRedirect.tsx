import { Navigate, useParams } from 'react-router-dom'
import { useEntitiesByType } from '@/hooks/useEntities'
import { snapshotField } from '@/lib/formatters'

export default function LegacyFilingRedirect({ formCode }: { formCode: '720' | '721' }) {
  const { taxYear } = useParams<{ taxYear: string }>()
  const filings = useEntitiesByType('tax_filing', { limit: 100 })

  if (filings.isLoading) {
    return <p className="text-sm text-muted-foreground animate-pulse py-8">Loading filing…</p>
  }

  const match = (filings.data?.entities ?? []).find((entity) => {
    const entityForm = String(snapshotField<string>(entity.snapshot, 'form_code') ?? '')
    const entityYear = String(snapshotField<number | string>(entity.snapshot, 'tax_year') ?? '')
    return entityForm === formCode && entityYear === taxYear
  })

  return <Navigate to={match ? `/filings/${match.entity_id}` : '/filings'} replace />
}
