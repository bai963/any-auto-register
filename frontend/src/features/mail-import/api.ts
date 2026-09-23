import { apiFetch } from '@/lib/utils'
import { toImportApiType, type MailImportSelectionType } from './selection'

export type MailImportSnapshot = {
  type: 'applemail' | 'microsoft'
  label: string
  count: number
  items: Array<{ index: number; email: string; mailbox: string; enabled?: boolean | null; status?: string; has_oauth?: boolean | null; account_type?: 'microsoft_oauth' | 'mailapi_url' | null }>
  truncated: boolean
  filename: string
  path: string
  pool_dir: string
}

export type MailImportResult = {
  type: 'applemail' | 'microsoft'
  summary: { total: number; success: number; failed: number }
  snapshot: MailImportSnapshot
  errors: string[]
  meta: Record<string, unknown>
}

export async function fetchMailImportProviders() {
  return apiFetch('/mail-imports/providers')
}

export async function fetchMailImportSnapshot(args: { selection: MailImportSelectionType; poolDir: string; poolFile: string }) {
  const apiType = toImportApiType(args.selection)
  const params = new URLSearchParams({ type: apiType })
  if (apiType === 'applemail') {
    if (args.poolDir.trim()) params.set('pool_dir', args.poolDir.trim())
    if (args.poolFile.trim()) params.set('pool_file', args.poolFile.trim())
  }
  return apiFetch(`/mail-imports/snapshot?${params.toString()}`) as Promise<MailImportSnapshot>
}

export async function importMail(body: Record<string, unknown>) {
  return apiFetch('/mail-imports', { method: 'POST', body: JSON.stringify(body) }) as Promise<MailImportResult>
}

export async function deleteImportedMail(body: Record<string, unknown>) {
  return apiFetch('/mail-imports/delete', { method: 'POST', body: JSON.stringify(body) }) as Promise<MailImportResult>
}

export async function batchDeleteImportedMail(body: Record<string, unknown>) {
  return apiFetch('/mail-imports/batch-delete', { method: 'POST', body: JSON.stringify(body) }) as Promise<MailImportResult>
}

export async function persistMailImportSource(source: MailImportSelectionType) {
  return apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data: { mail_import_source: source } }) })
}
