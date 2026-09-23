import type { Account, JsonRecord, NormalizedAccount, UsageWindow } from './types'

export function parseExtraJson(raw: string | undefined): JsonRecord {
  if (!raw) return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as JsonRecord)
      : {}
  } catch {
    return {}
  }
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as JsonRecord)
    : {}
}

export function normalizeAccount(account: Account): NormalizedAccount {
  const extra = parseExtraJson(account.extra_json)
  const syncStatuses = asRecord(extra.sync_statuses)
  return {
    ...account,
    extra,
    cpaSync: asRecord(syncStatuses.cpa),
    sub2apiSync: asRecord(syncStatuses.sub2api),
    cliproxySync: asRecord(syncStatuses.cliproxyapi),
    chatgptLocal: asRecord(extra.chatgpt_local),
    plusCheck: asRecord(extra.plus_check),
    totpSecret: String(extra.totp_secret || ''),
  }
}

export function formatSyncTime(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

export function formatCreatedAt(value?: string) {
  if (!value) return { date: '-', time: '' }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return { date: value, time: '' }
  return {
    date: date.toLocaleDateString(),
    time: date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
  }
}

export function formatUsageResetDate(value: unknown): string {
  if (!value) return ''
  const raw = String(value).trim()
  if (!raw) return ''
  const numeric = Number(raw)
  const date = Number.isFinite(numeric)
    ? new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric)
    : new Date(raw)
  if (Number.isNaN(date.getTime())) return raw
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

export function usageWindowText(window: UsageWindow | undefined): string {
  const remaining = Number(window?.remaining_percent)
  if (!Number.isFinite(remaining)) return '-'
  const reset = formatUsageResetDate(window?.reset_at)
  return `${remaining.toFixed(remaining % 1 === 0 ? 0 : 1)}%${reset ? ` · 重置 ${reset}` : ''}`
}

export function usageTagColor(window: UsageWindow | undefined): string {
  const remaining = Number(window?.remaining_percent)
  if (!Number.isFinite(remaining)) return 'default'
  if (remaining <= 0) return 'error'
  if (remaining < 20) return 'warning'
  return 'success'
}
