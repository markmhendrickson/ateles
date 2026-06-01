import { useCallback, useMemo, useState } from 'react'
import { Download } from 'lucide-react'
import { useEntitiesByType } from '@/hooks/useEntities'
import { useObservationHydratedFinancialAccounts } from '@/hooks/useObservationHydratedFinancialAccounts'
import { useEntityFxRates, useFilingYearEndFxRate } from '@/hooks/useEntityFxRates'
import { exportModeloWorkbook } from '@/lib/modeloExcelExport'
import { collectModeloScope, findModeloCoverageIssues } from '@/lib/modeloScope'
import { fetchFrankfurterEurUsdForDate } from '@/lib/frankfurterClient'
import type { FilingYear } from '@/constants/filingYears'
import { Button } from '@/components/ui/button'

export default function DownloadModeloButton({ filingYear }: { filingYear: FilingYear }) {
  const [busy, setBusy] = useState(false)
  const { data } = useEntitiesByType('financial_account')
  const { entities: hydratedEntities } = useObservationHydratedFinancialAccounts(data?.entities, filingYear)

  const scope = useMemo(
    () => collectModeloScope(hydratedEntities ?? data?.entities ?? [], filingYear),
    [hydratedEntities, data?.entities, filingYear],
  )
  const yearEndRate = useFilingYearEndFxRate(filingYear)
  const { resolveUsdPerEur } = useEntityFxRates(scope.allRows, {
    fallbackUsdPerEur: yearEndRate,
  })

  const handleDownload = useCallback(async () => {
    setBusy(true)
    try {
      const issues = findModeloCoverageIssues(scope)
      if (issues.length > 0) {
        const preview = issues
          .slice(0, 8)
          .map((i) => `- ${i.registryId || i.entityId}: ${i.issue}`)
          .join('\n')
        alert(
          `Data coverage check failed (${issues.length} issue${issues.length === 1 ? '' : 's'}).` +
            `\n\nFix these before export:\n${preview}` +
            (issues.length > 8 ? `\n...and ${issues.length - 8} more.` : ''),
        )
        return
      }

      const fxResult = await fetchFrankfurterEurUsdForDate(`${filingYear}-12-31`)
      const ecbRate = 1 / fxResult.usdPerEur

      await exportModeloWorkbook({
        taxYear: filingYear,
        accounts720: scope.accounts720,
        accounts721: scope.accounts721,
        equityRows: scope.equityRows,
        resolveUsdPerEur,
        ecbRate,
      })
    } catch (err) {
      console.error('Excel export failed:', err)
      alert(`Export failed: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
    }
  }, [filingYear, scope, resolveUsdPerEur])

  const totalAccounts = scope.allRows.length
  const countBreakdown = `720: ${scope.accounts720.length}, equity: ${scope.equityRows.length}, 721: ${scope.accounts721.length}`

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={handleDownload}
      disabled={busy || totalAccounts === 0}
      className="gap-2"
      title={`Combined scope: ${countBreakdown}`}
    >
      <Download size={14} />
      {busy ? 'Generating...' : `Download Excel (${totalAccounts})`}
    </Button>
  )
}
