import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { TooltipProvider } from '@/components/ui/tooltip'
import { FxRateProvider } from '@/context/FxRateContext'
import { DisplayUnitProvider } from '@/context/DisplayUnitContext'
import { MaskModeProvider } from '@/context/MaskModeContext'
import Layout from '@/components/Layout'
import Overview from '@/pages/Overview'
import Accounts from '@/pages/Accounts'
import AccountDetail from '@/pages/AccountDetail'
import FilingsLayout from '@/pages/FilingsLayout'
import FilingsIndex from '@/pages/FilingsIndex'
import FilingDetail from '@/pages/FilingDetail'
import LegacyFilingRedirect from '@/pages/LegacyFilingRedirect'
import Loans from '@/pages/Loans'
import RecurringExpenses from '@/pages/RecurringExpenses'
import Transactions from '@/pages/Transactions'
import Timeline from '@/pages/Timeline'
import EntityExplorer from '@/pages/EntityExplorer'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

/** Match Vite `base` so in-app links work when the app is served from a subpath. */
function routerBasename(): string | undefined {
  const b = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
  return b === '' ? undefined : b
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={350}>
        <FxRateProvider>
          <DisplayUnitProvider>
            <MaskModeProvider>
              <BrowserRouter
                basename={routerBasename()}
                future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
              >
                <Routes>
                  <Route element={<Layout />}>
                    <Route index element={<Overview />} />
                    <Route path="accounts" element={<Accounts />} />
                    <Route path="accounts/:id" element={<AccountDetail />} />
                    <Route path="filings" element={<FilingsLayout />}>
                      <Route index element={<FilingsIndex />} />
                      <Route path="modelo-720/:taxYear" element={<LegacyFilingRedirect formCode="720" />} />
                      <Route path="modelo-721/:taxYear" element={<LegacyFilingRedirect formCode="721" />} />
                      <Route path=":id" element={<FilingDetail />} />
                    </Route>
                    <Route path="modelo-720/*" element={<Navigate to="/filings" replace />} />
                    <Route path="modelo-721/*" element={<Navigate to="/filings" replace />} />
                    <Route path="loans" element={<Loans />} />
                    <Route path="recurring-expenses" element={<RecurringExpenses />} />
                    <Route path="transactions" element={<Transactions />} />
                    <Route path="timeline" element={<Timeline />} />
                    <Route path="explorer" element={<EntityExplorer />} />
                  </Route>
                </Routes>
              </BrowserRouter>
            </MaskModeProvider>
          </DisplayUnitProvider>
        </FxRateProvider>
      </TooltipProvider>
    </QueryClientProvider>
  )
}
