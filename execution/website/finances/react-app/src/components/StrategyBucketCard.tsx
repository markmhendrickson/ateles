import { useMaskMode } from '@/context/MaskModeContext'
import type { BucketAggregate } from '@/lib/aggregations'
import { humanizePropertyKey } from '@/lib/propertyLabels'
import { formatEur, formatUsd } from '@/lib/formatters'
import { AggregateMonetaryPair } from '@/components/MonetaryPair'
import { Card, CardContent, CardHeader } from '@/components/ui/card'

interface Props {
  bucket: BucketAggregate
  totalNetWorthEur: number
  /** Sum of per-entity canonical EUR × entity USD/EUR rate within this bucket. */
  bucketTotalUsd: number
}

export default function StrategyBucketCard({ bucket, totalNetWorthEur, bucketTotalUsd }: Props) {
  const { enabled: maskOn, text, maskCount, maskPercent, maskMoney } = useMaskMode()
  const pct = totalNetWorthEur > 0 ? (bucket.totalEur / totalNetWorthEur) * 100 : 0
  const readable = humanizePropertyKey(bucket.bucket)
  const label = maskOn ? text(readable) : readable
  const pctShown = maskOn ? maskPercent(Math.round(pct * 10) / 10, 'bucket-pct') : pct
  const countShown = maskOn ? maskCount(bucket.count, 'bucket-n') : bucket.count

  return (
    <Card>
      <CardHeader className="space-y-0 pb-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium" title={bucket.bucket}>
            {label}
          </h3>
          <span className="text-xs text-muted-foreground">
            {countShown} account{countShown !== 1 ? 's' : ''}
          </span>
        </div>
      </CardHeader>
      <CardContent>
        <AggregateMonetaryPair
          eurLabel={formatEur(maskOn ? maskMoney(bucket.totalEur, `sb-e:${bucket.bucket}`) : bucket.totalEur)}
          usdLabel={formatUsd(maskOn ? maskMoney(bucketTotalUsd, `sb-u:${bucket.bucket}`) : bucketTotalUsd)}
          align="left"
          primaryClassName="text-2xl font-semibold tracking-tight"
        />
        <div className="mt-2">
          <div className="h-1.5 rounded-full bg-muted overflow-hidden">
            <div
              className="h-full rounded-full bg-primary transition-all"
              style={{ width: `${Math.min(maskOn ? pctShown : pct, 100)}%` }}
            />
          </div>
          <p className="text-xs text-muted-foreground mt-1">{pctShown.toFixed(1)}% of total</p>
        </div>
      </CardContent>
    </Card>
  )
}
