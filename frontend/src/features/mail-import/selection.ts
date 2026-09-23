import { normalizeMailImportSource, type MailImportSource } from '@/lib/mailImport'

export type MailImportProviderType = 'applemail' | 'microsoft'
export type MailImportSelectionType = MailImportSource

export interface MailImportProviderDescriptor {
  type: MailImportProviderType
  label: string
  description: string
  content_placeholder: string
  helper_text: string
  supports_filename: boolean
  filename_label: string
  filename_placeholder: string
  preview_empty_text: string
}

export interface MailImportDisplayProvider extends Omit<MailImportProviderDescriptor, 'type'> {
  type: MailImportSelectionType
  apiType: MailImportProviderType
}

export interface MailImportSnapshotItem {
  index: number
  email: string
  mailbox: string
  enabled?: boolean | null
  status?: string
  has_oauth?: boolean | null
  account_type?: 'microsoft_oauth' | 'mailapi_url' | null
}

export const SUPPORTED_IMPORT_TYPES: MailImportProviderType[] = ['applemail', 'microsoft']

export function isSupportedImportType(value: string): value is MailImportProviderType {
  return SUPPORTED_IMPORT_TYPES.includes(value as MailImportProviderType)
}

export function toImportApiType(value: MailImportSelectionType): MailImportProviderType {
  return value === 'applemail' ? 'applemail' : 'microsoft'
}

export function resolvePreferredImportType(
  currentMailProvider: string,
  mailImportSource: string,
): MailImportSelectionType | null {
  return mailImportSource ? normalizeMailImportSource(mailImportSource, currentMailProvider) : null
}

export function buildDisplayProviders(providers: MailImportProviderDescriptor[]) {
  const items: MailImportDisplayProvider[] = []
  for (const provider of providers) {
    if (provider.type === 'applemail') {
      items.push({ ...provider, type: 'applemail', apiType: 'applemail', label: 'AppleMail / 小苹果' })
      continue
    }
    items.push(
      {
        ...provider, type: 'outlook', apiType: 'microsoft', label: 'Outlook',
        description: '导入 Outlook 本地号池，支持 mixed 导入（OAuth / MailAPI URL）；选中这一栏后注册取号只会取 OAuth 账号，走 Graph/IMAP 收码。',
        helper_text: '支持自动识别：邮箱----密码----client_id----refresh_token 或 邮箱----mailapi_url；当前视图仅展示 @outlook 的 OAuth 账号。',
        content_placeholder: 'example@outlook.com----password----client_id----refresh_token',
        preview_empty_text: '当前还没有可预览的 Outlook 已导入账号。',
      },
      {
        ...provider, type: 'hotmail', apiType: 'microsoft', label: 'Hotmail',
        description: '导入 Hotmail 本地号池；注册时和 Outlook 一样只取 OAuth 账号，走 Graph/IMAP 收码。',
        helper_text: '支持邮箱----密码----client_id----refresh_token；当前视图仅展示 @hotmail 的 OAuth 账号。',
        content_placeholder: 'example@hotmail.com----password----client_id----refresh_token',
        preview_empty_text: '当前还没有可预览的 Hotmail 已导入账号。',
      },
      {
        ...provider, type: 'mailapi', apiType: 'microsoft', label: 'MailAPI URL',
        description: '导入 MailAPI URL 账号池；运行时通过对应 URL 轮询验证码。',
        helper_text: '格式：邮箱----mailapi_url；该视图不进行 OAuth 可用性检测。',
        content_placeholder: 'example@hotmail.com----https://mailapi.example/key',
        preview_empty_text: '当前还没有可预览的 MailAPI URL 已导入账号。',
      },
    )
  }
  return items
}

export function matchesSelectionType(
  selection: MailImportSelectionType,
  email: string,
  accountType?: string | null,
): boolean {
  const domain = String(email.split('@')[1] || '').trim().toLowerCase()
  const normalizedType = String(accountType || 'microsoft_oauth').trim().toLowerCase()
  if (selection === 'mailapi') return normalizedType === 'mailapi_url'
  if (selection === 'hotmail') return normalizedType !== 'mailapi_url' && domain.includes('hotmail')
  if (selection === 'outlook') return normalizedType !== 'mailapi_url' && domain.includes('outlook')
  return true
}

export function filterSnapshotBySelection<T extends { type: MailImportProviderType; items: MailImportSnapshotItem[] }>(
  snapshot: T | null,
  selection: MailImportSelectionType,
): T | null {
  if (!snapshot || selection === 'applemail' || snapshot.type !== 'microsoft') return snapshot
  return { ...snapshot, items: snapshot.items.filter((item) => matchesSelectionType(selection, item.email, item.account_type)) }
}
