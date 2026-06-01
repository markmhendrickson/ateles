import { Outlet } from 'react-router-dom'

export default function FilingsLayout() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Filings</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Filing entities plus account-based filing scope views
        </p>
      </div>

      <Outlet />
    </div>
  )
}
