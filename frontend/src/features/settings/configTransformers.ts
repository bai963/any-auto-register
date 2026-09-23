import { parseBooleanConfigValue } from '@/lib/configValueParsers'

export function formatResultText(data: unknown) {
  if (typeof data === 'string') return data
  try {
    return JSON.stringify(data, null, 2)
  } catch {
    return String(data)
  }
}

export function normalizeDomainList(input: unknown): string[] {
  const items = Array.isArray(input) ? input : []
  const seen = new Set<string>()
  const domains: string[] = []
  for (const item of items) {
    const domain = String(item || '').trim().toLowerCase().replace(/^@/, '')
    if (!domain || seen.has(domain)) continue
    seen.add(domain)
    domains.push(domain)
  }
  return domains
}

export function parseStoredDomainList(value: unknown): string[] {
  if (Array.isArray(value)) return normalizeDomainList(value)
  if (typeof value !== 'string') return []
  const text = value.trim()
  if (!text) return []
  try {
    const parsed: unknown = JSON.parse(text)
    if (Array.isArray(parsed)) return normalizeDomainList(parsed)
  } catch {
    // Older configuration values are comma/newline delimited instead of JSON.
  }
  return normalizeDomainList(
    text.split('\n').flatMap((line) => line.split(',')).map((item) => item.trim()),
  )
}

export function resolveFeatureEnabledConfig(value: unknown, fallbackEnabled: boolean): boolean {
  const normalized = String(value ?? '').trim()
  return normalized ? parseBooleanConfigValue(normalized) : fallbackEnabled
}
