import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Layout from '@/components/Layout'
import TaskList from '@/pages/TaskList'
import TaskDetail from '@/pages/TaskDetail'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

function routerBasename(): string | undefined {
  const b = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
  return b === '' ? undefined : b
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter
        basename={routerBasename()}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<TaskList />} />
            <Route path="tasks/:id" element={<TaskDetail />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
