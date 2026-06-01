import { saveAs } from 'file-saver'
import { buildModeloWorkbookBuffer } from './modeloWorkbookBuilder'
import type { ModeloExportOptions } from './modeloWorkbookBuilder'

export async function exportModeloWorkbook(options: ModeloExportOptions): Promise<void> {
  const buffer = await buildModeloWorkbookBuffer(options)
  const blob = new Blob([buffer], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  })
  saveAs(blob, `Modelos_720_721_${options.taxYear}.xlsx`)
}
