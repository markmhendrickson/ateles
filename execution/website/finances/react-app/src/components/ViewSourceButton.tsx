import { useCallback, useState } from 'react'
import { createPortal } from 'react-dom'
import { resolveSourceContentUrl } from '@/api/sources'

interface Props {
  sourceId: string | null | undefined
  sourceHint?: string | null
  className?: string
  /** Render as inline text link instead of a button */
  inline?: boolean
  label?: string
}

export default function ViewSourceButton({ sourceId, sourceHint, className, inline, label }: Props) {
  const [viewerOpen, setViewerOpen] = useState(false)
  const [viewerUrl, setViewerUrl] = useState<string>('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleClick = useCallback(
    async (e: React.MouseEvent) => {
      e.preventDefault()
      e.stopPropagation()
      if (!sourceId) return

      setError(null)
      setLoading(true)
      try {
        const resolved = await resolveSourceContentUrl(sourceId, sourceHint)
        setViewerUrl(resolved)
        setViewerOpen(true)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to open source viewer')
      } finally {
        setLoading(false)
      }
    },
    [sourceHint, sourceId],
  )

  if (!sourceId) return null

  const text = loading ? 'Loading…' : label ?? 'View source'

  const viewerModal =
    viewerOpen && viewerUrl
      ? createPortal(
          <div
            className="fixed inset-0 z-[100] bg-black/60 p-4 md:p-8"
            onClick={() => setViewerOpen(false)}
          >
            <div
              className="mx-auto flex h-full w-full max-w-6xl flex-col overflow-hidden rounded-lg border bg-background shadow-xl"
              onClick={e => e.stopPropagation()}
            >
              <div className="flex items-center justify-between border-b px-4 py-2">
                <p className="text-sm font-medium">Source viewer</p>
                <button
                  type="button"
                  className="text-sm text-muted-foreground hover:text-foreground"
                  onClick={() => setViewerOpen(false)}
                >
                  Close
                </button>
              </div>
              <div className="flex-1 bg-muted/20">
                <iframe
                  src={viewerUrl}
                  title="Source file viewer"
                  className="h-full w-full"
                />
              </div>
              <div className="flex items-center justify-end gap-4 border-t px-4 py-2 text-xs">
                <a
                  href={viewerUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="text-primary underline underline-offset-2 hover:no-underline"
                >
                  Open in new tab
                </a>
              </div>
            </div>
          </div>,
          document.body,
        )
      : null

  if (inline) {
    return (
      <>
        <a
          href="#"
          onClick={handleClick}
          className={
            className ??
            'text-primary underline underline-offset-2 hover:no-underline text-xs'
          }
          title={error ?? 'Open source in embedded viewer'}
        >
          {text}
        </a>
        {viewerModal}
      </>
    )
  }

  return (
    <>
      <button
        type="button"
        onClick={handleClick}
        className={
          className ??
          'inline-flex items-center gap-1 text-xs text-primary hover:underline underline-offset-2 disabled:opacity-50'
        }
        title={error ?? 'Open source in embedded viewer'}
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="shrink-0"
        >
          <path d="M15 3h6v6" />
          <path d="M10 14 21 3" />
          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
        </svg>
        {text}
      </button>
      {viewerModal}
    </>
  )
}
