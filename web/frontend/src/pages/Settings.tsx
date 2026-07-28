import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, CheckCircle2, Clock3, Plus, Power, RefreshCw, RotateCw, Save, Trash2, TriangleAlert, Undo2, XCircle } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import { adminApi, type RestartRequiredStatus, type VersionCheck } from '../adminApi'
import { Badge, Card, CardHeader, EmptyState, SectionTitle, Subtabs } from '../components/UI'

type Tab = 'runtime' | 'providers' | 'startup' | 'version'
type RestartUiPhase = 'idle' | 'running' | 'succeeded' | 'failed'
type HeaderDraft = { id: string; name: string; value: string }
type ProviderDraft = {
  baseUrl: string
  apiKey: string
  headers: HeaderDraft[]
  originalConfig: Record<string, unknown>
}

let headerSequence = 0
const nextHeaderId = () => `provider-header-${headerSequence += 1}`

function draftFromConfig(config: Record<string, unknown>): ProviderDraft {
  const rawHeaders = config.default_headers
  const headers = rawHeaders && !Array.isArray(rawHeaders) && typeof rawHeaders === 'object'
    ? Object.entries(rawHeaders).map(([name, value]) => ({ id: nextHeaderId(), name, value: String(value ?? '') }))
    : []
  return {
    baseUrl: typeof config.base_url === 'string' ? config.base_url : '',
    apiKey: '',
    headers,
    originalConfig: { ...config },
  }
}

function draftsFrom(configs: Record<string, Record<string, unknown>>, ids: string[]) {
  return Object.fromEntries(ids.map(id => [id, draftFromConfig(configs[id] ?? {})]))
}

const restartGroupLabels: Record<string, string> = {
  environment: '环境变量',
  startup: '启动与重启入口',
  backend: '网关后端',
  frontend: '网页构建',
  providers: 'Provider 代码或清单',
  dependencies: '依赖声明',
  version: '版本文件',
}

function restartFileDescription(path: string): string {
  if (path === '.env') return '环境变量文件（仅展示路径，不展示任何变量值）'
  if (path === 'start_web.py' || path === 'restart.py') return '启动或平滑重启入口'
  if (path.startsWith('providers/')) return 'Provider 代码、清单或静态资源'
  if (path.startsWith('web/frontend/')) return '网页源代码、构建产物或前端配置'
  if (path.startsWith('web/backend/')) return 'Web 管理后端'
  if (path.startsWith('storage/')) return '调用统计存储后端'
  if (path.startsWith('api/') || path.startsWith('core/')) return '网关后端运行代码'
  if (path === 'version.json') return '网关版本声明'
  return '启动时加载的项目文件'
}

function formatRestartDuration(milliseconds: number): string {
  const seconds = milliseconds / 1000
  return seconds < 10 ? `${seconds.toFixed(1)} 秒` : `${Math.round(seconds)} 秒`
}

export default function Settings() {
  const { csrfToken: token, data, restart, isOwner, saveControl, saveProvider, requestRestart } = useAdmin()
  const [tab, setTab] = useState<Tab>('runtime')
  const [prompt, setPrompt] = useState(data.highest_priority_system_prompt)
  const [disabledProviders, setDisabledProviders] = useState(data.disabled_providers)
  const [providerDrafts, setProviderDrafts] = useState<Record<string, ProviderDraft>>(() => draftsFrom(data.provider_configs, data.providers.map(provider => provider.provider_id)))
  const [restartRequired, setRestartRequired] = useState<RestartRequiredStatus | null>(null)
  const [restartCheckBusy, setRestartCheckBusy] = useState(false)
  const [versionCheck, setVersionCheck] = useState<VersionCheck | null>(null)
  const [versionBusy, setVersionBusy] = useState(false)
  const [busy, setBusy] = useState('')
  const [saved, setSaved] = useState('')
  const [error, setError] = useState('')
  const [restartUiPhase, setRestartUiPhase] = useState<RestartUiPhase>('idle')
  const [restartRequestId, setRestartRequestId] = useState<string | null>(null)
  const [restartStartedAt, setRestartStartedAt] = useState<number | null>(null)
  const [restartElapsedMs, setRestartElapsedMs] = useState(0)
  const [lastRestartDurationMs, setLastRestartDurationMs] = useState<number | null>(null)
  const [restartConfirmOpen, setRestartConfirmOpen] = useState(false)
  const restartConfirmButtonRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    setPrompt(data.highest_priority_system_prompt)
    setDisabledProviders(data.disabled_providers)
    setProviderDrafts(current => {
      const next = { ...current }
      for (const provider of data.providers) {
        if (!next[provider.provider_id]) {
          next[provider.provider_id] = draftFromConfig(data.provider_configs[provider.provider_id] ?? {})
        }
      }
      return next
    })
  }, [data.highest_priority_system_prompt, data.disabled_providers, data.provider_configs, data.providers])

  const flashSaved = (message: string) => {
    setSaved(message)
    window.setTimeout(() => setSaved(''), 1800)
  }

  const loadRestartRequired = useCallback(async () => {
    setRestartCheckBusy(true)
    try {
      setRestartRequired(await adminApi.restartRequired(token))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法检测启动配置改动')
    } finally {
      setRestartCheckBusy(false)
    }
  }, [token])

  useEffect(() => {
    if (restartUiPhase !== 'running' || restartStartedAt === null) return
    const updateElapsed = () => setRestartElapsedMs(Date.now() - restartStartedAt)
    updateElapsed()
    const timer = window.setInterval(updateElapsed, 250)
    return () => window.clearInterval(timer)
  }, [restartStartedAt, restartUiPhase])

  useEffect(() => {
    if (
      restartUiPhase !== 'running'
      || restartStartedAt === null
      || restartRequestId === null
      || restart?.restart.request_id !== restartRequestId
    ) return
    if (restart.restart.phase === 'succeeded') {
      const duration = Math.max(0, Date.now() - restartStartedAt)
      setRestartElapsedMs(duration)
      setLastRestartDurationMs(duration)
      setRestartUiPhase('succeeded')
      void loadRestartRequired()
    } else if (restart.restart.phase === 'failed') {
      setRestartElapsedMs(Math.max(0, Date.now() - restartStartedAt))
      setRestartUiPhase('failed')
      setError(restart.restart.message || '网关重启失败')
    }
  }, [loadRestartRequired, restart, restartRequestId, restartStartedAt, restartUiPhase])

  useEffect(() => {
    if (!['succeeded', 'failed'].includes(restartUiPhase)) return
    const timer = window.setTimeout(() => setRestartUiPhase('idle'), 8000)
    return () => window.clearTimeout(timer)
  }, [restartUiPhase])

  useEffect(() => {
    if (!restartConfirmOpen) return
    restartConfirmButtonRef.current?.focus()
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setRestartConfirmOpen(false)
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [restartConfirmOpen])

  const loadVersionCheck = useCallback(async () => {
    setVersionBusy(true)
    try {
      setVersionCheck(await adminApi.versionCheck(token))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法检测版本')
    } finally {
      setVersionBusy(false)
    }
  }, [token])

  useEffect(() => {
    if (tab === 'startup' && restartRequired === null) void loadRestartRequired()
    if (tab === 'version' && versionCheck === null) void loadVersionCheck()
  }, [loadRestartRequired, loadVersionCheck, restartRequired, tab, versionCheck])

  const saveRuntimeSettings = async () => {
    setBusy('runtime'); setError('')
    try {
      await saveControl(prompt, disabledProviders, data.disabled_models)
      flashSaved('运行控制已保存')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败') }
    finally { setBusy('') }
  }

  const toggleProvider = (providerId: string) => {
    setDisabledProviders(current => current.includes(providerId) ? current.filter(value => value !== providerId) : [...current, providerId])
  }

  const updateDraft = (providerId: string, update: (draft: ProviderDraft) => ProviderDraft) => {
    setProviderDrafts(current => {
      const draft = current[providerId] ?? draftFromConfig(data.provider_configs[providerId] ?? {})
      return { ...current, [providerId]: update(draft) }
    })
  }

  const restoreProviderSettings = (providerId: string) => {
    setProviderDrafts(current => ({
      ...current,
      [providerId]: draftFromConfig(data.provider_configs[providerId] ?? {}),
    }))
    setError('')
    flashSaved(`${providerId} 已恢复为当前保存值`)
  }

  const saveProviderSettings = async (providerId: string) => {
    const draft = providerDrafts[providerId]
    if (!draft) return
    const baseUrl = draft.baseUrl.trim()
    if (!baseUrl) {
      setError(`${providerId} 的 Base URL 不能为空`)
      return
    }
    const headerNames = draft.headers.map(header => header.name.trim()).filter(Boolean)
    if (new Set(headerNames.map(name => name.toLowerCase())).size !== headerNames.length) {
      setError(`${providerId} 存在重复的请求头名称`)
      return
    }
    const defaultHeaders = Object.fromEntries(
      draft.headers
        .map(header => [header.name.trim(), header.value] as const)
        .filter(([name]) => Boolean(name)),
    )
    const config = {
      ...draft.originalConfig,
      base_url: baseUrl,
      default_headers: defaultHeaders,
    }
    setBusy(providerId); setError('')
    try {
      await saveProvider(providerId, config, draft.apiKey.trim() || undefined)
      const redactedConfig = {
        ...config,
        default_headers: Object.fromEntries(Object.keys(defaultHeaders).map(name => [name, ''])),
      }
      setProviderDrafts(current => ({ ...current, [providerId]: draftFromConfig(redactedConfig) }))
      flashSaved(`${providerId} API 配置已保存`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Provider 配置保存失败') }
    finally { setBusy('') }
  }

  const startRestart = async () => {
    const startedAt = Date.now()
    setBusy('restart'); setError('')
    setRestartStartedAt(startedAt)
    setRestartElapsedMs(0)
    setRestartRequestId(null)
    setRestartUiPhase('running')
    try {
      setRestartRequestId(await requestRestart('manual web restart', false))
    } catch (reason) {
      setRestartElapsedMs(Date.now() - startedAt)
      setRestartUiPhase('failed')
      setError(reason instanceof Error ? reason.message : '无法提交重启')
    }
    finally { setBusy('') }
  }

  const providerIds = data.providers.map(provider => provider.provider_id)
  const restartPhase = restart?.restart.phase ?? 'idle'
  const restartAvailable = ['idle', 'succeeded', 'failed'].includes(restartPhase)
  const versionTone = versionCheck?.status === 'update_available' ? 'warning' : versionCheck?.status === 'unavailable' ? 'danger' : 'success'

  return <>
    <Subtabs items={[
      { id: 'runtime', label: '运行控制' },
      { id: 'providers', label: 'Provider API 配置' },
      { id: 'startup', label: '启动与重启' },
      { id: 'version', label: '版本检测' },
    ]} value={tab} onChange={setTab}/>
    <SectionTitle
      title="系统设置"
      description="运行配置即时生效；启动配置与程序文件改动需要重启"
      action={<>{saved && <Badge tone="success"><Check size={13}/>{saved}</Badge>}{tab === 'runtime' && <button className="button" disabled={busy === 'runtime'} onClick={() => void saveRuntimeSettings()}><Save size={15}/>保存运行控制</button>}</>}
    />
    {error && <div className="global-alert" role="alert"><span>{error}</span><button onClick={() => setError('')}>×</button></div>}

    {tab === 'runtime' && <div className="settings-grid">
      <Card><CardHeader title="Provider 启停" description="启停会立即影响后续新请求" action={<Badge>无需重启</Badge>}/>{providerIds.length ? <div className="control-list provider-control-list">{providerIds.map(id => { const enabled = !disabledProviders.includes(id); return <label key={id}><span><b>{id}</b><small>{data.providers.find(provider => provider.provider_id === id)?.models.length ?? 0} 个模型</small></span><button className={`toggle ${enabled ? 'on' : ''}`} aria-label={`${enabled ? '关停' : '启动'} ${id}`} onClick={() => toggleProvider(id)}><i/></button></label> })}</div> : <p>没有已加载 Provider。</p>}</Card>
      <Card><CardHeader title="最高权限系统提示词" description="由网关注入，优先级高于调用方提示词" action={<Badge>无需重启</Badge>}/><div className="form-field"><textarea value={prompt} onChange={event => setPrompt(event.target.value)} rows={10} placeholder="留空表示不注入最高权限系统提示词"/><small>保存后仅影响新请求，不修改正在执行的响应。</small></div></Card>
    </div>}

    {tab === 'providers' && (!providerIds.length ? <EmptyState title="没有已加载 Provider" description="新增厂商目录后需重启网关，才会显示在这里。"/> : <div className="provider-settings-list">{providerIds.map(id => {
      const draft = providerDrafts[id] ?? draftFromConfig(data.provider_configs[id] ?? {})
      return <Card key={id} className="provider-config-card">
        <CardHeader title={id} description="连接信息与默认请求头" action={<Badge>无需重启</Badge>}/>
        <div className="provider-api-fields">
          <div className="form-field full"><label>Base URL</label><input type="url" value={draft.baseUrl} onChange={event => updateDraft(id, current => ({ ...current, baseUrl: event.target.value }))} placeholder="https://api.provider.example"/></div>
          <div className="form-field full"><label>API 密钥</label><input type="password" autoComplete="new-password" value={draft.apiKey} onChange={event => updateDraft(id, current => ({ ...current, apiKey: event.target.value }))} placeholder="留空则继续使用当前密钥"/><small>密钥只写不回显；恢复配置会清空此输入框，不会删除已保存密钥。</small></div>
          <div className="provider-headers full">
            <div className="provider-headers-title"><span>默认请求头</span><button className="btn" onClick={() => updateDraft(id, current => ({ ...current, headers: [...current.headers, { id: nextHeaderId(), name: '', value: '' }] }))}><Plus size={14}/>添加请求头</button></div>
            {draft.headers.length ? draft.headers.map(header => <div className="provider-header-row" key={header.id}>
              <input aria-label="请求头名称" value={header.name} onChange={event => updateDraft(id, current => ({ ...current, headers: current.headers.map(item => item.id === header.id ? { ...item, name: event.target.value } : item) }))} placeholder="Header 名称"/>
              <input type="password" autoComplete="new-password" aria-label="请求头值" value={header.value} onChange={event => updateDraft(id, current => ({ ...current, headers: current.headers.map(item => item.id === header.id ? { ...item, value: event.target.value } : item) }))} placeholder="留空则保留当前值"/>
              <button className="icon-button danger" aria-label={`删除 ${header.name || '请求头'}`} onClick={() => updateDraft(id, current => ({ ...current, headers: current.headers.filter(item => item.id !== header.id) }))}><Trash2 size={15}/></button>
            </div>) : <p className="empty-inline">未配置默认请求头</p>}
          </div>
        </div>
        <div className="card-actions provider-config-actions"><button className="btn" disabled={busy === id} onClick={() => restoreProviderSettings(id)}><Undo2 size={14}/>恢复配置</button><button className="btn primary" disabled={busy === id} onClick={() => void saveProviderSettings(id)}><Save size={14}/>保存配置</button></div>
      </Card>
    })}</div>)}

    {tab === 'startup' && <div className="startup-panel">
      <Card className={`restart-required-card ${restartRequired?.required ? 'requires-restart' : ''}`}>
        <div className="restart-required-copy">
          <span className="restart-status-icon"><RotateCw size={20}/></span>
          <div><h3>{restartRequired?.message ?? (restartCheckBusy ? '正在检测需要重启的改动…' : '尚未检测')}</h3>
            {restartRequired?.required && <p>{restartRequired.changed_groups.map(group => restartGroupLabels[group] ?? group).join('、')}</p>}
            {restartRequired && !restartRequired.required && <p>热更新配置不会触发此提示。</p>}
          </div>
        </div>
        <button className="btn" disabled={restartCheckBusy} onClick={() => void loadRestartRequired()}><RefreshCw size={14} className={restartCheckBusy ? 'spin' : ''}/>重新检测</button>
      </Card>
      <Card className={`restart-action-card restart-ui-${restartUiPhase}`}>
        <div className="restart-action-copy">
          <h3>{restartUiPhase === 'running' ? '正在重启网关' : restartUiPhase === 'succeeded' ? '网关已重新启动' : '重启网关'}</h3>
          {restartUiPhase === 'running' ? <p>当前阶段：{restartRequestId ? restartPhase : '正在提交'}，正在等待新实例通过健康检查。</p> : lastRestartDurationMs !== null ? <p className="restart-duration-result"><Clock3 size={13}/>本次重启耗时 <b>{formatRestartDuration(lastRestartDurationMs)}</b></p> : <p>{restart && !restartAvailable ? `当前重启阶段：${restartPhase}` : '等待当前请求完成后平滑重启。'}</p>}
        </div>
        {isOwner ? restartUiPhase === 'running' ? <span className="restart-progress-bubble"><RotateCw className="spin" size={15}/>重启中 · {Math.floor(restartElapsedMs / 1000)} 秒</span> : restartUiPhase === 'succeeded' ? <span className="restart-success-bubble"><CheckCircle2 size={16}/>重启成功</span> : restartUiPhase === 'failed' ? <span className="restart-failed-bubble"><XCircle size={16}/>重启失败</span> : <button className="button restart-trigger-button" aria-haspopup="dialog" disabled={busy === 'restart' || !restartAvailable} onClick={() => setRestartConfirmOpen(true)}><RotateCw size={15}/>重启</button> : <button className="button" disabled><Power size={15}/>需要 owner 权限</button>}
      </Card>
      {restartRequired?.required && <Card className="restart-change-details">
        <CardHeader title="需要重启的变更明细" description="以下项目相对当前进程启动时发生了变化；只展示路径，不展示文件内容或敏感值" action={<Badge tone="warning">{restartRequired.changed_files?.length ?? restartRequired.changed_groups.length} 项</Badge>}/>
        <div className="restart-change-list">
          {restartRequired.changed_files?.length ? restartRequired.changed_files.map(path => <div key={path}><code>{path}</code><span>{restartFileDescription(path)}</span></div>) : restartRequired.changed_groups.map(group => <div key={group}><code>{restartGroupLabels[group] ?? group}</code><span>该类别中的启动文件已发生变化</span></div>)}
        </div>
      </Card>}
    </div>}

    {tab === 'version' && <div className="version-panel">
      <Card className="version-summary-card">
        <div><span className="version-kicker">GitHub main / version.json</span><h3>{versionCheck?.message ?? (versionBusy ? '正在连接版本源…' : '尚未检测版本')}</h3><p>对比本地版本与远程正式版本，不会自动执行更新。</p></div>
        <div className="version-actions">{versionCheck && <Badge tone={versionTone}>{versionCheck.status === 'update_available' ? '需要更新' : versionCheck.status === 'unavailable' ? '检测失败' : '无需更新'}</Badge>}<button className="btn" disabled={versionBusy} onClick={() => void loadVersionCheck()}><RefreshCw size={14} className={versionBusy ? 'spin' : ''}/>{versionCheck ? '重新检测' : '开始检测'}</button></div>
      </Card>
      <div className="two-grid version-compare-grid">
        <Card><CardHeader title="本地版本" description="根目录 version.json"/><div className="version-number-row"><strong className="version-number">{versionCheck?.local?.version ?? '—'}</strong><span className="version-protocol"><small>协议</small><b>{versionCheck?.local?.protocol_version ?? '—'}</b></span></div></Card>
        <Card><CardHeader title="远程版本" description="kesepain-KE/kemo-adapter-api · main"/><div className="version-number-row"><strong className="version-number">{versionCheck?.remote?.version ?? '—'}</strong><span className="version-protocol"><small>协议</small><b>{versionCheck?.remote?.protocol_version ?? '—'}</b></span></div></Card>
      </div>
    </div>}
    {restartConfirmOpen && createPortal(<div className="restart-confirm-overlay" role="presentation" onPointerDown={event => { if (event.target === event.currentTarget) setRestartConfirmOpen(false) }}>
      <section className="restart-confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="restart-confirm-title" aria-describedby="restart-confirm-description">
        <span className="restart-confirm-icon"><TriangleAlert size={24}/></span>
        <div className="restart-confirm-copy">
          <span>高影响操作</span>
          <h3 id="restart-confirm-title">确认重启网关？</h3>
          <p id="restart-confirm-description">重启期间新请求可能短暂不可用；系统会等待当前请求完成，并在新实例通过健康检查后恢复服务。</p>
        </div>
        <div className="restart-confirm-actions">
          <button className="btn" onClick={() => setRestartConfirmOpen(false)}>取消</button>
          <button ref={restartConfirmButtonRef} className="button restart-confirm-button" onClick={() => { setRestartConfirmOpen(false); void startRestart() }}><RotateCw size={15}/>确认重启</button>
        </div>
      </section>
    </div>, document.body)}
  </>
}
