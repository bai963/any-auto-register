import { useEffect, useMemo, useState } from 'react'
import type { Key } from 'react'
import { fetchMailImportProviders, fetchMailImportSnapshot, type MailImportSnapshot } from './api'
import {
  buildDisplayProviders,
  filterSnapshotBySelection,
  isSupportedImportType,
  resolvePreferredImportType,
  type MailImportDisplayProvider,
  type MailImportProviderDescriptor,
  type MailImportSelectionType,
} from './selection'

type MessageApi = { error: (content: string) => void }

export function useMailImportSnapshot({
  currentMailProvider, storedMailImportSource, poolDir, poolFile, message,
}: {
  currentMailProvider: string
  storedMailImportSource: string
  poolDir: string
  poolFile: string
  message: MessageApi
}) {
  const [providers, setProviders] = useState<MailImportDisplayProvider[]>([])
  const [selectedType, setSelectedType] = useState<MailImportSelectionType>('outlook')
  const [loadingProviders, setLoadingProviders] = useState(false)
  const [loadingSnapshot, setLoadingSnapshot] = useState(false)
  const [rawSnapshot, setRawSnapshot] = useState<MailImportSnapshot | null>(null)
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([])
  const providerMap = useMemo(() => new Map(providers.map((item) => [item.type, item])), [providers])
  const selectedProvider = providerMap.get(selectedType) ?? null
  const preferredImportType = useMemo(
    () => resolvePreferredImportType(currentMailProvider, storedMailImportSource),
    [currentMailProvider, storedMailImportSource],
  )
  const snapshot = useMemo(() => filterSnapshotBySelection(rawSnapshot, selectedType), [rawSnapshot, selectedType])
  const tableData = useMemo(() => (snapshot?.items || []).map((item) => ({ ...item, key: `${item.email}::${item.mailbox || ''}` })), [snapshot])

  const loadProviders = async () => {
    setLoadingProviders(true)
    try {
      const data = await fetchMailImportProviders() as { items?: MailImportProviderDescriptor[] }
      const items = Array.isArray(data.items) ? data.items.filter((item) => isSupportedImportType(item.type)) : []
      const displayProviders = buildDisplayProviders(items)
      setProviders(displayProviders)
      const available = new Set(displayProviders.map((item) => item.type))
      setSelectedType((current) => preferredImportType && available.has(preferredImportType) ? preferredImportType : available.has(current) ? current : displayProviders[0]?.type ?? current)
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载邮箱导入配置失败')
    } finally { setLoadingProviders(false) }
  }
  const loadSnapshot = async (selection: MailImportSelectionType) => {
    setLoadingSnapshot(true)
    try { setRawSnapshot(await fetchMailImportSnapshot({ selection, poolDir, poolFile })) }
    catch { setRawSnapshot(null) }
    finally { setLoadingSnapshot(false) }
  }
  useEffect(() => { void loadProviders() }, [])
  useEffect(() => { if (preferredImportType && providerMap.has(preferredImportType)) setSelectedType(preferredImportType) }, [preferredImportType, providerMap])
  useEffect(() => { if (selectedProvider) void loadSnapshot(selectedType) }, [selectedProvider, selectedType, poolDir, poolFile])
  useEffect(() => { setSelectedRowKeys([]) }, [selectedType, rawSnapshot])
  return { providers, selectedType, setSelectedType, selectedProvider, loadingProviders, loadingSnapshot, rawSnapshot, setRawSnapshot, snapshot, tableData, selectedRowKeys, setSelectedRowKeys, loadSnapshot }
}
