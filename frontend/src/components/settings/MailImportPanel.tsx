import { useMemo, useState } from 'react'
import { App, Alert, Button, Card, Form, Input, InputNumber, Popconfirm, Select, Space, Switch, Table, Tag, Typography } from 'antd'
import type { FormInstance } from 'antd'

import { useStoredMailImportSource } from '@/lib/mailImport'
import {
  batchDeleteImportedMail,
  deleteImportedMail,
  importMail,
  persistMailImportSource as persistMailImportSourceRequest,
} from '@/features/mail-import/api'
import { useMailImportSnapshot } from '@/features/mail-import/useMailImportSnapshot'
import { toImportApiType, type MailImportProviderType, type MailImportSelectionType, type MailImportSnapshotItem } from '@/features/mail-import/selection'

interface MailImportPanelProps {
  form: FormInstance
}

interface MailImportSnapshot {
  type: MailImportProviderType
  label: string
  count: number
  items: MailImportSnapshotItem[]
  truncated: boolean
  filename: string
  path: string
  pool_dir: string
}

interface MailImportSummary {
  total: number
  success: number
  failed: number
}

interface MailImportResult {
  type: MailImportProviderType
  summary: MailImportSummary
  snapshot: MailImportSnapshot
  errors: string[]
  meta: Record<string, unknown>
}

function buildImportSuccessMessage(result: MailImportResult) {
  if (result.type === 'applemail') {
    const fileLabel = result.snapshot.filename ? `，已绑定 ${result.snapshot.filename}` : ''
    return `导入成功，共 ${result.summary.success} 个邮箱${fileLabel}`
  }
  return `导入完成：成功 ${result.summary.success} / 失败 ${result.summary.failed}`
}

function buildResultMessage(result: MailImportResult) {
  if (result.type === 'applemail') {
    return `导入完成：成功 ${result.summary.success} / 失败 ${result.summary.failed}`
  }
  return `导入完成：成功 ${result.summary.success} / 失败 ${result.summary.failed}`
}

export default function MailImportPanel({ form }: MailImportPanelProps) {
  const { message } = App.useApp()
  const currentMailProvider = String(Form.useWatch('mail_provider', form) || '')
  const storedMailImportSource = useStoredMailImportSource(form)
  const watchedPoolDir = String(Form.useWatch('applemail_pool_dir', form) || 'mail')
  const watchedPoolFile = String(Form.useWatch('applemail_pool_file', form) || '')

  const {
    providers, selectedType, setSelectedType, selectedProvider, loadingProviders,
    loadingSnapshot, rawSnapshot, setRawSnapshot, snapshot, tableData,
    selectedRowKeys, setSelectedRowKeys, loadSnapshot,
  } = useMailImportSnapshot({
    currentMailProvider, storedMailImportSource, poolDir: watchedPoolDir, poolFile: watchedPoolFile, message,
  })
  const [content, setContent] = useState('')
  const [filename, setFilename] = useState('')
  const [importing, setImporting] = useState(false)
  const [deletingEmail, setDeletingEmail] = useState('')
  const [batchDeleting, setBatchDeleting] = useState(false)
  const [result, setResult] = useState<MailImportResult | null>(null)
  const [aliasSplitEnabled, setAliasSplitEnabled] = useState(false)
  const [aliasSplitCount, setAliasSplitCount] = useState(5)
  const [aliasIncludeOriginal, setAliasIncludeOriginal] = useState(false)

  const selectedApiType = selectedProvider?.apiType ?? toImportApiType(selectedType)
  const supportsAliasSplit = selectedApiType === 'microsoft'

  /**
   * 视图选择立刻落库，不等整页「保存」。用户在这里选完 MailAPI URL 就去别的页面
   * 是常态，只留在表单里等于没选。
   */
  const persistImportSource = async (value: MailImportSelectionType) => {
    try {
      await persistMailImportSourceRequest(value)
    } catch {
      // 表单里那份还在，点「保存配置」还能补上；但得说一声，否则刷新回来又变 Outlook
      // 会看起来像界面自己乱跳
      message.warning('这一栏没能保存，刷新后可能变回 Outlook，点一下「保存配置」再试')
    }
  }

  const handleImport = async () => {
    const payload = content.trim()
    if (!payload) {
      message.error('请输入导入内容')
      return
    }

    setImporting(true)
    try {
      const apiType = toImportApiType(selectedType)
      const body: Record<string, unknown> = {
        type: apiType,
        content: payload,
        enabled: true,
        bind_to_config: true,
      }

      if (apiType === 'applemail') {
        body.filename = filename.trim()
        body.pool_dir = String(form.getFieldValue('applemail_pool_dir') || 'mail').trim() || 'mail'
      } else {
        body.alias_split_enabled = aliasSplitEnabled
        body.alias_split_count = aliasSplitCount
        body.alias_include_original = aliasIncludeOriginal
      }

      const response = await importMail(body)

      setResult(response)
      setRawSnapshot(response.snapshot)
      setContent('')
      setFilename('')

      if (response.type === 'applemail') {
        form.setFieldsValue({
          mail_provider: 'mail_import',
          mail_import_source: 'applemail',
          applemail_pool_dir: response.snapshot.pool_dir,
          applemail_pool_file: response.snapshot.filename,
        })
        void persistImportSource('applemail')
      } else if (response.type === 'microsoft') {
        form.setFieldsValue({
          mail_provider: 'mail_import',
          mail_import_source: selectedType,
        })
        void persistImportSource(selectedType)
      }

      message.success(buildImportSuccessMessage(response))
    } catch (error) {
      const detail = error instanceof Error ? error.message : '邮箱导入失败'
      message.error(detail)
    } finally {
      setImporting(false)
    }
  }

  const handleTypeChange = (value: MailImportSelectionType) => {
    setSelectedType(value)
    form.setFieldsValue({
      mail_provider: 'mail_import',
      mail_import_source: value,
    })
    void persistImportSource(value)
  }

  const handleDelete = async (item: MailImportSnapshotItem) => {
    const apiType = toImportApiType(selectedType)
    const email = String(item.email || '').trim()
    if (!email) return

    setDeletingEmail(email)
    try {
      const body: Record<string, unknown> = {
        type: apiType,
        email,
      }

      if (apiType === 'applemail') {
        body.mailbox = item.mailbox || ''
        body.pool_dir = String(form.getFieldValue('applemail_pool_dir') || 'mail').trim() || 'mail'
        body.pool_file = String(form.getFieldValue('applemail_pool_file') || '').trim()
      }

      const response = await deleteImportedMail(body)

      setResult(response)
      setRawSnapshot(response.snapshot)
      setSelectedRowKeys([])
      message.success(`已删除 ${email}`)
    } catch (error) {
      const detail = error instanceof Error ? error.message : '删除失败'
      message.error(detail)
    } finally {
      setDeletingEmail('')
    }
  }

  const handleBatchDelete = async () => {
    if (!selectedRowKeys.length) {
      message.warning('请先勾选要删除的邮箱')
      return
    }

    const selectedItems = tableData.filter((item) => selectedRowKeys.includes(item.key))
    if (!selectedItems.length) {
      message.warning('未找到要删除的邮箱')
      return
    }

    const apiType = toImportApiType(selectedType)
    setBatchDeleting(true)
    try {
      const body: Record<string, unknown> = {
        type: apiType,
        items: selectedItems.map((item) => ({
          email: item.email,
          mailbox: item.mailbox || '',
        })),
      }

      if (apiType === 'applemail') {
        body.pool_dir = String(form.getFieldValue('applemail_pool_dir') || 'mail').trim() || 'mail'
        body.pool_file = String(form.getFieldValue('applemail_pool_file') || '').trim()
      }

      const response = await batchDeleteImportedMail(body)

      setResult(response)
      setRawSnapshot(response.snapshot)
      setSelectedRowKeys([])
      message.success(`批量删除完成：成功 ${response.summary.success} / 失败 ${response.summary.failed}`)
    } catch (error) {
      const detail = error instanceof Error ? error.message : '批量删除失败'
      const shouldFallbackToSingleDelete = /405|404|Method Not Allowed|Not Found/i.test(detail)

      if (!shouldFallbackToSingleDelete) {
        message.error(detail)
        return
      }

      let success = 0
      let failed = 0
      const errors: string[] = []

      for (const item of selectedItems) {
        try {
          const body: Record<string, unknown> = {
            type: apiType,
            email: item.email,
          }

          if (apiType === 'applemail') {
            body.mailbox = item.mailbox || ''
            body.pool_dir = String(form.getFieldValue('applemail_pool_dir') || 'mail').trim() || 'mail'
            body.pool_file = String(form.getFieldValue('applemail_pool_file') || '').trim()
          }

          const response = await deleteImportedMail(body)

          setResult(response)
          setRawSnapshot(response.snapshot)
          success += 1
        } catch (singleError) {
          failed += 1
          errors.push(singleError instanceof Error ? singleError.message : `删除失败: ${item.email}`)
        }
      }

      setSelectedRowKeys([])
      if (errors.length) {
        message.warning(`批量删除已回退单条删除：成功 ${success} / 失败 ${failed}`)
        setResult((prev) => prev ? {
          ...prev,
          errors,
          summary: { total: success + failed, success, failed },
        } : prev)
      } else {
        message.success(`批量删除已回退单条删除：成功 ${success} / 失败 ${failed}`)
      }
    } finally {
      setBatchDeleting(false)
    }
  }

  const columns = useMemo(() => {
    const baseColumns = [
      {
        title: '#',
        dataIndex: 'index',
        key: 'index',
        width: 72,
      },
      {
        title: '邮箱',
        dataIndex: 'email',
        key: 'email',
      },
    ]

    if (selectedType === 'applemail') {
      baseColumns.push({
        title: '邮箱文件夹',
        dataIndex: 'mailbox',
        key: 'mailbox',
        width: 140,
        render: (value: string) => <Tag>{value || 'INBOX'}</Tag>,
      } as never)
    } else {
      baseColumns.push(
        {
          title: '类型',
          dataIndex: 'account_type',
          key: 'account_type',
          width: 120,
          render: (value: string | null | undefined) => {
            const isMailApi = String(value || '').trim().toLowerCase() === 'mailapi_url'
            return <Tag color={isMailApi ? 'purple' : 'blue'}>{isMailApi ? 'MailAPI URL' : 'OAuth'}</Tag>
          },
        } as never,
        {
          title: '状态',
          dataIndex: 'status',
          key: 'status',
          width: 110,
          render: (value: string | null | undefined) => {
            const labels: Record<string, { color: string; text: string }> = {
              available: { color: 'green', text: '未使用' },
              in_use: { color: 'processing', text: '使用中' },
              used: { color: 'blue', text: '已使用' },
              failed: { color: 'red', text: '失败' },
            }
            const item = labels[String(value || 'available')] || labels.available
            return <Tag color={item.color}>{item.text}</Tag>
          },
        } as never,
        {
          title: '启用',
          dataIndex: 'enabled',
          key: 'enabled',
          width: 80,
          render: (value: boolean | null | undefined) => (
            <Tag color={value ? 'default' : 'default'}>{value ? '是' : '否'}</Tag>
          ),
        } as never,
        {
          title: '认证',
          dataIndex: 'has_oauth',
          key: 'has_oauth',
          width: 100,
          render: (value: boolean | null | undefined) => (
            <Tag color={value ? 'blue' : 'default'}>{value ? 'OAuth' : '密码'}</Tag>
          ),
        } as never,
      )
    }

    baseColumns.push({
      title: '操作',
      key: 'action',
      width: 90,
      render: (_: unknown, item: MailImportSnapshotItem) => (
        <Popconfirm
          title="确认删除这个邮箱吗？"
          description={item.email}
          okText="删除"
          cancelText="取消"
          okButtonProps={{ danger: true, loading: deletingEmail === item.email }}
          onConfirm={() => void handleDelete(item)}
        >
          <Button
            danger
            type="link"
            size="small"
            loading={deletingEmail === item.email}
            style={{ paddingInline: 0 }}
          >
            删除
          </Button>
        </Popconfirm>
      ),
    } as never)

    return baseColumns
  }, [deletingEmail, selectedType, tableData])

  return (
    <Card
      title="邮箱导入"
      extra={(
        <Select
          value={selectedType}
          onChange={handleTypeChange}
          loading={loadingProviders}
          style={{ width: 240 }}
          options={providers.map((provider) => ({
            label: provider.label,
            value: provider.type,
          }))}
        />
      )}
      style={{ marginBottom: 16 }}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        <Typography.Text type="secondary">
          {selectedProvider?.description || '通过统一导入接口，将内容导入到对应邮箱账号池。'}
        </Typography.Text>
        {selectedProvider?.helper_text ? (
          <Typography.Text type="secondary">{selectedProvider.helper_text}</Typography.Text>
        ) : null}

        {selectedProvider?.supports_filename ? (
          <Form.Item label={selectedProvider.filename_label || '文件名'} style={{ marginBottom: 0 }}>
            <Input
              value={filename}
              onChange={(event) => setFilename(event.target.value)}
              placeholder={selectedProvider.filename_placeholder}
            />
          </Form.Item>
        ) : null}

        {supportsAliasSplit ? (
          <div
            style={{
              border: '1px dashed var(--border-strong)',
              borderRadius: 8,
              padding: 12,
              display: 'flex',
              flexDirection: 'column',
              gap: 10,
            }}
          >
            <Space align="center">
              <Typography.Text strong>邮箱裂变（别名）</Typography.Text>
              <Switch checked={aliasSplitEnabled} onChange={setAliasSplitEnabled} />
              <Typography.Text type="secondary">
                默认关闭；开启后每个原邮箱生成随机 6 位英文别名
              </Typography.Text>
            </Space>
            {aliasSplitEnabled ? (
              <Space align="center" wrap>
                <Typography.Text>每个原邮箱裂变数量</Typography.Text>
                <InputNumber
                  min={1}
                  max={5}
                  value={aliasSplitCount}
                  onChange={(value) => setAliasSplitCount(Math.max(1, Math.min(5, Number(value || 5))))}
                />
                <Typography.Text type="secondary">（1~5）</Typography.Text>
                <Typography.Text style={{ marginLeft: 16 }}>包含原邮箱</Typography.Text>
                <Switch checked={aliasIncludeOriginal} onChange={setAliasIncludeOriginal} />
              </Space>
            ) : null}
          </div>
        ) : null}

        <Input.TextArea
          value={content}
          onChange={(event) => setContent(event.target.value)}
          rows={10}
          placeholder={selectedProvider?.content_placeholder || ''}
          style={{ fontFamily: 'monospace' }}
        />

        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Button
            danger
            onClick={() => {
              setContent('')
              setFilename('')
              setResult(null)
            }}
          >
            清空
          </Button>
          <Space>
            <Button onClick={() => void loadSnapshot(selectedType)} loading={loadingSnapshot}>
              刷新预览
            </Button>
            <Button type="primary" onClick={handleImport} loading={importing}>
              确认导入
            </Button>
          </Space>
        </Space>

        {result ? (
          <Alert
            type={result.summary.failed ? 'warning' : 'success'}
            showIcon
            message={buildResultMessage(result)}
            description={result.errors.length ? (
              <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{result.errors.join('\n')}</pre>
            ) : undefined}
          />
        ) : null}

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <Tag color="blue">
            {selectedType === 'applemail'
              ? `已导入: ${snapshot?.count || 0} 个邮箱`
              : `当前预览匹配: ${snapshot?.items.length || 0}${rawSnapshot?.truncated ? ` / 总池 ${rawSnapshot?.count || 0}` : ''}`}
          </Tag>
          {selectedType === 'applemail' && snapshot?.filename ? (
            <Typography.Text type="secondary">当前文件: {snapshot.filename}</Typography.Text>
          ) : null}
          {snapshot?.items?.length ? (
            <Popconfirm
              title={`确认删除已勾选的 ${selectedRowKeys.length} 个邮箱吗？`}
              okText="批量删除"
              cancelText="取消"
              okButtonProps={{ danger: true, loading: batchDeleting }}
              onConfirm={() => void handleBatchDelete()}
              disabled={!selectedRowKeys.length}
            >
              <Button danger disabled={!selectedRowKeys.length} loading={batchDeleting}>
                批量删除
              </Button>
            </Popconfirm>
          ) : null}
        </div>
        {snapshot?.items?.length ? (
          <Table
            rowSelection={{
              selectedRowKeys,
              onChange: setSelectedRowKeys,
            }}
            columns={columns}
            dataSource={tableData}
            size="small"
            pagination={false}
            scroll={{ y: 320 }}
          />
        ) : (
          <div
            style={{
              border: '1px solid var(--border)',
              borderRadius: 8,
              padding: 12,
              background: 'var(--bg-subtle)',
              minHeight: 88,
              display: 'flex',
              alignItems: 'center',
            }}
          >
            <Typography.Text type="secondary">
              {selectedProvider?.preview_empty_text || '当前还没有可预览的导入内容。'}
            </Typography.Text>
          </div>
        )}

        {snapshot?.truncated ? (
          <Typography.Text type="secondary">预览只展示前 100 条记录，完整内容以实际存储为准。</Typography.Text>
        ) : null}
      </Space>
    </Card>
  )
}
