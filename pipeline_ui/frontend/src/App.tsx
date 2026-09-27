import { AppLayout } from "@/components/AppLayout"
import { PipelineOverview } from "@/components/PipelineOverview"
import { PreferencesPanel } from "@/components/PreferencesPanel"
import { ThemeProvider } from "@/components/theme-provider"

function App() {
  return (
    <ThemeProvider defaultTheme="claro">
      <AppLayout aside={<PreferencesPanel />}>
        <PipelineOverview />
      </AppLayout>
    </ThemeProvider>
  )
}

export default App
