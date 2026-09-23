export type JsonRecord = Record<string, unknown>

export type SyncStatus = {
  success?: boolean
  status?: string
  message?: string
  updated_at?: string
  [key: string]: unknown
}

export type UsageWindow = {
  remaining_percent?: number
  reset_at?: string | number
}

export type Account = {
  id: number
  email?: string
  extra_json?: string
  [key: string]: unknown
}

export type NormalizedAccount = Account & {
  extra: JsonRecord
  cpaSync: SyncStatus
  sub2apiSync: SyncStatus
  cliproxySync: SyncStatus
  chatgptLocal: JsonRecord
  plusCheck: JsonRecord
  totpSecret: string
}
