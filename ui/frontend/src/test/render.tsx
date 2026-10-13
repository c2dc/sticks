import type { ReactElement } from "react"
import { render, type RenderOptions } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"

import i18n from "@/i18n"

/**
 * Render a component wrapped in the app's i18n provider so `useTranslation`
 * resolves real pt-BR strings (Task 12.5). The instance defaults to pt-BR.
 */
export function renderWithI18n(ui: ReactElement, options?: RenderOptions) {
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>, options)
}
