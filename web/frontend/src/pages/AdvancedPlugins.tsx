import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ArrowUpRight, Download, Expand, Maximize2, Minimize2, Package, Plus, RefreshCw, Search, Shield, Upload } from 'lucide-react'
import { api, useAction, useConsole } from '../console'
import type { Entity } from '../console'
import { Empty, ErrorBar, Field } from '../components/Form'
import { Modal, PageHeader } from '../components/UI'
import { Select } from '../components/Select'
import { PluginField, PluginFrame } from '../components/PluginConsole'
import { t } from '../i18n'

const states: Record<string, string> = {
  installed: '已安装', ready: '运行中', needs_config: '待配置', needs_auth: '待授权',
  starting: '启动中', degraded: '运行异常', disabled: '已停用', updating: '更新中', failed: '启动失败',
}
const categories: Record<string, string> = { model_account: '模型与账号接入', cli_provider: 'CLI Provider', provider_extension: '其他 Provider 扩展' }
const permissions: Record<string, string> = {
  'model.register': '注册插件模型', 'model.invoke': '执行模型请求', 'process.cli.execute': '运行本机 CLI',
  'account.status.read': '读取插件账户状态', 'account.authorize': '执行账户授权动作',
  'plugin.config.write': '保存插件配置', 'network.access': '访问外部网络',
}

export default function AdvancedPlugins() {
  const { data, refresh } = useConsole()
  const action = useAction()
  const [params, setParams] = useSearchParams()
  const [inventory, setInventory] = useState<Entity | null>(null)
  const [catalog, setCatalog] = useState<Entity | null>(null)
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('all')
  const [status, setStatus] = useState('all')
  const [pending, setPending] = useState<{ file?: File; market?: Entity; preview: Entity } | null>(null)
  const [grant, setGrant] = useState<Entity | null>(null)
  const [grants, setGrants] = useState<string[]>([])
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const [sources, setSources] = useState<Entity[]>([])
  const [expanded, setExpanded] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const workspace = useRef<HTMLDivElement>(null)
  const tab = params.get('view') || 'installed'
  const canManage = !!data?.permissions.can_manage_extensions
  const load = useCallback(async () => { setInventory(await api('/advanced-plugins')) }, [])
  const changed = useCallback(async () => { await load(); await refresh() }, [load, refresh])
  useEffect(() => {
    void action.run(load)
    const interval = window.setInterval(() => { if (!document.hidden) void action.run(load) }, 10000)
    return () => window.clearInterval(interval)
  }, [load])
  useEffect(() => {
    if (['market', 'updates'].includes(tab)) void action.run(async () => { setCatalog(await api('/advanced-plugins/catalog')); await load() })
  }, [tab, load])
  useEffect(() => {
    const listener = () => setFullscreen(document.fullscreenElement === workspace.current)
    document.addEventListener('fullscreenchange', listener)
    return () => document.removeEventListener('fullscreenchange', listener)
  }, [])
  const select = (key: string, value: string) => setParams(current => {
    const next = new URLSearchParams(current); next.set(key, value); return next
  })
  const matches = (item: Entity) => (category === 'all' || item.category === category)
    && `${item.name} ${item.id} ${item.summary} ${item.category} ${item.capabilities?.join(' ') || ''} ${item.permissions?.requested?.join(' ') || ''}`.toLowerCase().includes(query.toLowerCase())
  const items: Entity[] = (inventory?.plugins || []).filter((item: Entity) => matches(item) && (status === 'all' || status === item.status))
  const selected = inventory?.plugins.find((item: Entity) => item.id === params.get('plugin')) || items[0]
  const mutate = async (identity: string, operation: string, value: Entity = {}) => {
    await api(`/advanced-plugins/${encodeURIComponent(identity)}${operation === 'uninstall' ? '' : '/' + operation}`, {
      method: operation === 'uninstall' ? 'DELETE' : 'POST',
      body: JSON.stringify({ expected_revision: inventory!.revision, value }),
    })
    await changed()
  }
  const preview = async (file: File) => {
    const form = new FormData(); form.set('file', file)
    setPending({ file, preview: await api('/advanced-plugins/preview', { method: 'POST', body: form }) })
  }
  return <div className={`page advanced-plugins-page${expanded ? ' expanded' : ''}`}>
    <PageHeader title="高级插件" desc="接入独立的模型与账号来源，通过统一 Kemo 协议调用。"
      actions={<>
        <button className="btn" disabled={action.busy} onClick={() => { void action.run(changed) }}><RefreshCw size={14} />{t('刷新')}</button>
        <button className="btn" disabled={!canManage} onClick={() => { setSources(inventory?.sources || []); setSourcesOpen(true) }}>{t('插件源')}</button>
        <button className="btn btn-primary" disabled={!canManage || action.busy} onClick={() => input.current?.click()}><Plus size={14} />{t('导入插件')}</button>
      </>} />
    <input hidden type="file" accept=".zip" ref={input} onChange={event => {
      const file = event.target.files?.[0]
      if (file) void action.run(() => preview(file))
      event.target.value = ''
    }} />
    <ErrorBar error={action.error} />
    <div className="page-tabs" role="tablist" aria-label={t('高级插件')}>
      {[['installed', '已安装'], ['market', '插件市场'], ['updates', '可更新'], ['diagnostics', '诊断记录']].map(([value, label]) =>
        <button key={value} role="tab" aria-selected={tab === value} onClick={() => select('view', value)}>
          {t(label)}{value === 'installed' && <span className="tag plugin-tab-count">{inventory?.plugins.length ?? 0}</span>}
        </button>)}
    </div>
    <div className="toolbar plugin-toolbar">
      <div className="search"><Search size={15} /><input className="input" aria-label={t('搜索插件')} placeholder={t('搜索名称、类型或能力')} value={query} onChange={event => setQuery(event.target.value)} /></div>
      <Select aria-label={t('插件分类')} value={category} onChange={setCategory}
        options={[{ value: 'all', label: t('全部分类') }, ...Object.entries(categories).map(([value, label]) => ({ value, label: t(label) }))]} />
      <Select aria-label={t('运行状态')} value={status} onChange={setStatus}
        options={[{ value: 'all', label: t('全部状态') }, ...Object.entries(states).map(([value, label]) => ({ value, label: t(label) }))]} />
    </div>
    {!inventory ? <Empty text="正在加载" /> : ['market', 'updates'].includes(tab) ? <>
      {catalog?.errors.map((error: Entity) => <ErrorBar key={error.source_id} error={error.message} />)}
      <div className="plugin-market-grid">
        {(catalog?.items || []).filter((item: Entity) => matches(item) && (tab !== 'updates'
          || inventory.plugins.some((installed: Entity) => installed.id === item.id && installed.update?.version === item.version))).map((item: Entity) => {
          const installed = inventory.plugins.find((plugin: Entity) => plugin.id === item.id)
          return <article className="card plugin-market-card" key={`${item.source_id}:${item.id}:${item.version}`}>
            <div className="plugin-market-icon"><Package size={23} /></div>
            <div><h2>{item.name}</h2><p className="small muted">{item.author || item.source_name} · v{item.version}</p></div>
            <p>{item.summary || t('此插件未提供说明')}</p>
            <div className="row-actions"><span className="tag">{t(categories[item.category] || item.category || '其他 Provider 扩展')}</span><span className="small muted">{item.compatibility}</span>
              {item.capabilities?.map((capability: string) => <span className="tag" key={capability}>{capability}</span>)}</div>
            <button className="btn" disabled={!canManage || action.busy || installed?.version === item.version} onClick={() => { void action.run(async () => {
              const result = await api('/advanced-plugins/catalog/preview', { method: 'POST', body: JSON.stringify({ expected_revision: inventory.revision, value: item }) })
              setPending({ market: item, preview: result })
            }) }}><Download size={14} />{t(installed?.version === item.version ? '已安装' : installed ? '查看更新' : '查看并安装')}</button>
          </article>
        })}
      </div>
      {!catalog?.items.length && <div className="card plugin-market-empty"><Package size={30} /><h2>{t('尚无可用插件源')}</h2>
        <p>{t('添加符合 SDK v1 索引规范的插件源，或从本地导入 ZIP 插件包。')}</p>
        <div className="row-actions"><button className="btn" disabled={!canManage} onClick={() => { setSources(inventory.sources); setSourcesOpen(true) }}>{t('添加插件源')}</button>
          <button className="btn btn-primary" disabled={!canManage} onClick={() => input.current?.click()}><Upload size={14} />{t('导入插件')}</button></div></div>}
    </> : <div ref={workspace} className={`plugins-workbench${fullscreen ? ' fullscreen' : ''}`}>
      <aside className="plugin-sidebar">
        <div className="provider-sidebar-head"><span>{t('插件库存')}</span><span className="tag">{items.length}</span></div>
        <nav className="plugin-list card" aria-label={t('已安装插件')}>
          {items.map(item => <button key={item.id} className={`plugin-list-item${selected?.id === item.id ? ' selected' : ''}`}
            onClick={() => select('plugin', item.id)} aria-current={selected?.id === item.id ? 'page' : undefined}>
            <span className="plugin-list-icon"><Package size={18} /></span><span className="plugin-list-copy">
              <strong>{item.name}</strong><span>v{item.version} · {item.model_count} {t('模型')}</span></span>
            <span className={`plugin-status-dot ${item.status}`} title={t(states[item.status] || item.status)} />
          </button>)}
          {!items.length && <Empty text={inventory.plugins.length ? '没有匹配的插件' : '尚未安装插件'} />}
        </nav>
      </aside>
      <section className="plugin-detail card">
        {selected ? <>
          <div className="plugin-detail-head"><div><div className="row-actions"><h2>{selected.name}</h2><span className={`tag ${selected.status === 'ready' ? 'green' : ''}`}>{t(states[selected.status] || selected.status)}</span></div>
            <p className="small muted">v{selected.version} · {selected.author || selected.source} · {selected.model_count} {t('个可用模型')}</p></div>
            <div className="row-actions">
              <button className="icon-btn" title={t(expanded ? '恢复宽度' : '展开工作台')} aria-label={t(expanded ? '恢复宽度' : '展开工作台')} onClick={() => setExpanded(value => !value)}>{expanded ? <Minimize2 size={16} /> : <Expand size={16} />}</button>
              <button className="icon-btn" title={t(fullscreen ? '退出全屏' : '全屏')} aria-label={t(fullscreen ? '退出全屏' : '全屏')} onClick={() => { void action.run(async () => {
                if (document.fullscreenElement) await document.exitFullscreen(); else await workspace.current?.requestFullscreen()
              }) }}><Maximize2 size={16} /></button>
              <button className="btn btn-sm" disabled={!canManage || action.busy} onClick={() => { void action.run(() => mutate(selected.id, 'reload')) }}>{t('重新加载')}</button>
              <button className="btn btn-sm" disabled={!canManage || action.busy} onClick={() => {
                if (selected.enabled) void action.run(() => mutate(selected.id, 'disable'))
                else { setGrant(selected); setGrants([]) }
              }}>{t(selected.enabled ? '停用' : '启用')}</button>
            </div>
          </div>
          {selected.error && <div className="plugin-inline-error"><ErrorBar error={`${selected.error.code} · ${selected.error.message}`} /></div>}
          <PluginDetail key={`${selected.id}:${selected.version}`} plugin={selected} revision={inventory.revision} canManage={canManage}
            diagnosticsOnly={tab === 'diagnostics'} onChanged={changed} onMutate={mutate}
            onRemove={() => { if (window.confirm(t('卸载插件并撤销模型？已保存的配置与历史调用记录将保留。'))) void action.run(() => mutate(selected.id, 'uninstall')) }} />
        </> : <div className="plugin-welcome"><span className="plugin-welcome-icon"><Package size={36} /></span>
          <h2>{t('为网关接入更多模型来源')}</h2><p>{t('安装插件后，在这里管理账户、模型、能力与运行配置。')}</p>
          <div className="row-actions"><button className="btn" onClick={() => select('view', 'market')}>{t('浏览插件市场')}<ArrowUpRight size={14} /></button>
            <button className="btn btn-primary" disabled={!canManage} onClick={() => input.current?.click()}>{t('导入本地插件')}</button></div></div>}
      </section>
    </div>}
    <Modal open={!!pending} onClose={() => setPending(null)} title="插件包确认" width={620}>
      {pending && <><h2>{pending.preview.manifest.name} <span className="tag">v{pending.preview.manifest.version}</span></h2><p>{pending.preview.manifest.summary}</p>
        <p className="hint">{pending.preview.manifest.author || t('未知作者')} · {t(pending.preview.signature === 'unsigned' ? '未签名来源，请确认作者可信' : '已校验')}</p>
        <p className="small mono wrap-code">SHA-256 · {pending.preview.sha256}</p>
        <p>{t('插件将在独立进程中运行。进程隔离不构成操作系统安全沙箱，只应启用可信插件。')}</p>
        <div className="plugin-permissions">{pending.preview.manifest.permissions.requested.map((permission: string) =>
          <div key={permission}><Shield size={14} /><span>{t(permissions[permission] || permission)}</span><code>{permission}</code></div>)}</div>
        <p className="hint">{t('安装后先配置，再明确授权启用；安装不会自动执行新插件。')}</p>
        <ErrorBar error={action.error} /><div className="editor-actions"><button className="btn" onClick={() => setPending(null)}>{t('取消')}</button>
          <button className="btn btn-primary" disabled={action.busy} onClick={() => { void action.run(async () => {
            const update = inventory!.plugins.some((item: Entity) => item.id === pending.preview.manifest.id)
            if (pending.file) {
              const form = new FormData(); form.set('file', pending.file); form.set('expected_revision', inventory!.revision)
              form.set('sha256', pending.preview.sha256); form.set('update', String(update))
              await api('/advanced-plugins/install', { method: 'POST', body: form })
            } else await api('/advanced-plugins/catalog/install', { method: 'POST', body: JSON.stringify({ expected_revision: inventory!.revision, value: { ...pending.market, update } }) })
            await changed()
            setParams(current => {
              const next = new URLSearchParams(current)
              next.set('plugin', pending.preview.manifest.id); next.set('view', 'installed')
              return next
            })
            setPending(null)
          }) }}>{t(inventory?.plugins.some((item: Entity) => item.id === pending.preview.manifest.id) ? '确认更新' : '确认安装')}</button></div></>}
    </Modal>
    <Modal open={!!grant} onClose={() => setGrant(null)} title="授予插件权限">
      <p>{t('启用后，插件代码将以当前网关运行用户的身份执行。')}</p>
      <div className="plugin-grant-list">{grant?.permissions.requested.map((permission: string) => <label key={permission} className="check-label">
        <input type="checkbox" checked={grants.includes(permission)} onChange={event => setGrants(value => event.target.checked ? [...value, permission] : value.filter(item => item !== permission))} />
        <span>{t(permissions[permission] || permission)}<code>{permission}</code></span></label>)}</div>
      <ErrorBar error={action.error} /><div className="editor-actions"><button className="btn btn-primary" disabled={action.busy || grants.length !== grant?.permissions.requested.length}
        onClick={() => { void action.run(async () => { await mutate(grant!.id, 'enable', { grants }); setGrant(null) }) }}>{t('授权并启用')}</button></div>
    </Modal>
    <Modal open={sourcesOpen} onClose={() => setSourcesOpen(false)} title="插件源管理" width={720}>
      <div className="plugin-source-list">{sources.map((source, index) => <div className="edit-section" key={source.id || index}>
        <Field label="名称"><input className="input" value={source.name} onChange={event => setSources(value => value.map((item, i) => i === index ? { ...item, name: event.target.value } : item))} /></Field>
        <Field label="索引地址"><input className="input" type="url" value={source.url} placeholder="https://example.com/kemo-plugins.json"
          onChange={event => setSources(value => value.map((item, i) => i === index ? { ...item, url: event.target.value } : item))} /></Field>
        <div className="row-actions">{[['enabled', '启用'], ['allow_private_network', '允许私网地址']].map(([key, label]) => <label className="check-label" key={key}>
          <input type="checkbox" checked={!!source[key]} onChange={event => setSources(value => value.map((item, i) => i === index ? { ...item, [key]: event.target.checked } : item))} />{t(label)}</label>)}
          <button className="btn btn-sm" onClick={() => setSources(value => value.filter((_, i) => i !== index))}>{t('移除')}</button></div>
      </div>)}</div>
      <button className="btn" onClick={() => setSources(value => [...value, { name: '', url: '', enabled: true, allow_private_network: false }])}><Plus size={14} />{t('添加插件源')}</button>
      <ErrorBar error={action.error} /><div className="editor-actions"><button className="btn btn-primary" disabled={action.busy} onClick={() => { void action.run(async () => {
        await api('/advanced-plugins/sources', { method: 'PUT', body: JSON.stringify({ expected_revision: inventory!.revision, value: { sources } }) })
        await changed(); setCatalog(await api('/advanced-plugins/catalog')); setSourcesOpen(false)
      }) }}>{t('保存')}</button></div>
    </Modal>
  </div>
}

function PluginDetail({ plugin, revision, canManage, diagnosticsOnly, onChanged, onMutate, onRemove }: {
  plugin: Entity; revision: string; canManage: boolean; diagnosticsOnly: boolean
  onChanged: () => Promise<void>; onMutate: (id: string, operation: string, value?: Entity) => Promise<void>; onRemove: () => void
}) {
  const action = useAction()
  const [tab, setTab] = useState('overview')
  const [config, setConfig] = useState<Entity | null>(null)
  const [draft, setDraft] = useState<Entity>({})
  const [models, setModels] = useState<Entity[]>([])
  const [diagnostics, setDiagnostics] = useState<Entity | null>(null)
  const [result, setResult] = useState<Entity | null>(null)
  const [inputs, setInputs] = useState<Record<string, Entity>>({})
  const base = '/advanced-plugins/' + plugin.id
  const load = useCallback(async () => {
    const [next, list] = await Promise.all([api(base + '/config'), api(base + '/models')])
    setConfig(next); setDraft(next.values); setModels(list.models)
  }, [base])
  useEffect(() => { void action.run(load) }, [load, revision])
  useEffect(() => { if (diagnosticsOnly) setTab('diagnostics') }, [diagnosticsOnly])
  useEffect(() => { if (tab === 'diagnostics' || tab === 'config') void action.run(async () => setDiagnostics(await api(base + '/diagnostics'))) }, [tab, base])
  const pages: Entity[] = plugin.ui.pages.length ? plugin.ui.pages : plugin.ui.entry ? [{ id: 'console', title: '专属控制台', entry: plugin.ui.entry }] : []
  const tabs = [['overview', '总览'], ...pages.map(page => ['page:' + page.id, page.title]), ['models', '模型与能力'], ['config', '通用配置'], ['diagnostics', '诊断']]
  const page = pages.find(page => 'page:' + page.id === tab)
  const save = async (values = draft) => {
    const clean = { ...values }
    for (const [key, schema] of Object.entries(config!.schema.properties || {}) as [string, Entity][]) {
      if (schema.writeOnly && clean[key] === '') delete clean[key]
      if (['array', 'object'].includes(schema.type) && typeof clean[key] === 'string') clean[key] = JSON.parse(clean[key])
    }
    await api(base + '/config', { method: 'PATCH', body: JSON.stringify({ expected_revision: revision, value: clean }) })
    await load(); await onChanged()
  }
  const fields = Object.entries(config?.schema.properties || {}) as [string, Entity][]
  const groupedFields = new Set<string>((config?.panel?.containers || []).filter((container: Entity) => container.kind === 'config')
    .flatMap((container: Entity) => (container.fields || []).map((field: Entity) => field.key)))
  return <>
    <div className="plugin-detail-tabs" role="tablist">{tabs.map(([value, label]) => <button key={value} role="tab" aria-selected={value === tab} onClick={() => setTab(value)}>{t(label)}</button>)}</div>
    <ErrorBar error={action.error} />
    {pages.map(item => <div key={item.id} hidden={tab !== 'page:' + item.id} className="plugin-custom-page">
      <PluginFrame plugin={plugin} entry={item.entry} revision={revision} canManage={canManage} onChanged={onChanged} />
    </div>)}
    {!page && <div className="plugin-detail-body">
      {tab === 'overview' && <><p className="plugin-summary">{plugin.summary}</p>
        <div className="plugin-facts"><div><span>{t('运行模式')}</span><strong>{t('独立进程')} · RPC v1</strong></div>
          <div><span>{t('Provider')}</span><strong className="mono">{plugin.provider.provider_id}</strong></div>
          <div><span>{t('网关兼容版本')}</span><strong className="mono">{plugin.compatibility.gateway}</strong></div>
          <div><span>{t('来源')}</span><strong>{plugin.source}</strong></div></div>
        <h3>{t('已声明权限')}</h3><div className="plugin-permissions">{plugin.permissions.requested.map((permission: string) =>
          <div key={permission}><Shield size={14} /><span>{t(permissions[permission] || permission)}</span><code>{permission}</code></div>)}</div>
        <p className="hint">{t('代码更新保留配置、授权和历史记录；启用后模型立即对新请求生效。')}</p>
        <div className="row-actions"><button className="btn" onClick={() => setTab('config')}>{t('配置插件')}</button>
          {pages.length > 0 && <button className="btn" onClick={() => setTab('page:' + pages[0].id)}>{t('打开专属控制台')}<ArrowUpRight size={14} /></button>}
          {plugin.can_rollback && <button className="btn" disabled={!canManage || action.busy} onClick={() => { void action.run(async () => { await onMutate(plugin.id, 'rollback'); await load() }) }}>{t('恢复旧版本')}</button>}
          <button className="btn danger-text" disabled={!canManage || action.busy} onClick={onRemove}>{t('卸载插件')}</button></div>
      </>}
      {tab === 'models' && <><div className="row-actions"><button className="btn" disabled={!canManage || !plugin.enabled || action.busy} onClick={() => {
        if (plugin.category === 'cli_provider' && !window.confirm(t('探测将实际调用已授权的 CLI 模型，并可能消耗账户额度。继续？'))) return
        void action.run(async () => { await onMutate(plugin.id, 'probe'); await load() })
      }}>{t('探测并刷新模型')}</button><span className="hint">{t('只登记实际可用模型，未知能力不会默认开启。')}</span></div>
        {models.map(model => <details className="plugin-model-card" key={model.model}><summary><span className="mono">{model.model}</span>
          <span className="tag">{model.task}</span><span className="small muted">{model.input_modalities.join(' / ')} → {model.output_modalities.join(' / ')}</span></summary>
          <pre className="config-preview">{JSON.stringify(model, null, 2)}</pre></details>)}
        {!models.length && <Empty text="没有已验证可用的模型，请检查配置与授权后进行探测。" />}</>}
      {tab === 'config' && config && <form className="plugin-config-form" onSubmit={event => { event.preventDefault(); void action.run(() => save()) }}>
        <div className="form-grid two">{fields.filter(([name]) => !groupedFields.has(name)).map(([name, schema]) => <PluginField key={name} name={name} schema={schema} value={draft[name]} disabled={!canManage || action.busy}
          secretSet={config.secrets_set[name]} onChange={value => setDraft(current => ({ ...current, [name]: value }))} />)}</div>
        {Object.entries(config.secrets_set).filter(([, value]) => value).map(([name]) => <label key={name} className="check-label">
          <input type="checkbox" checked={draft[name] === null} disabled={!canManage || action.busy} onChange={event => setDraft(value => ({ ...value, [name]: event.target.checked ? null : undefined }))} />{t('清除此项凭据')} · {name}</label>)}
        <div className="plugin-panel-containers">{config.panel?.containers.map((container: Entity, index: number) =>
          container.kind === 'status' ? <section className="plugin-panel-section" key={index}><h3>{container.title}</h3><div className="plugin-facts">{container.fields?.map((field: Entity) =>
            <div key={field.key}><span>{field.label}</span><strong>{String(diagnostics?.runtime?.[field.key] ?? '—')}</strong></div>)}</div></section>
            : container.kind === 'config' ? <section key={index} className="plugin-panel-section"><h3>{container.title}</h3>
              <div className="form-grid two">{container.fields?.map((field: Entity) => <PluginField key={field.key} name={field.key}
                schema={config.schema.properties[field.key]} presentation={field} value={draft[field.key]}
                disabled={!canManage || action.busy} secretSet={config.secrets_set[field.key]}
                onChange={value => setDraft(current => ({ ...current, [field.key]: value }))} />)}</div><div className="row-actions">
              {container.presets?.map((preset: Entity) => <button key={preset.name} type="button" className="btn btn-sm" disabled={!canManage} onClick={() => setDraft(value => ({ ...value, ...preset.values }))}>{preset.name}</button>)}</div></section>
              : <section key={index} className="plugin-panel-section"><h3>{container.title}</h3>{container.controls?.map((control: Entity) => <div className="plugin-action-control" key={control.command}>
                {control.inputs?.map((field: Entity) => <PluginField key={field.key} name={field.key} schema={{
                  type: field.type === 'number' ? 'number' : field.type === 'boolean' ? 'boolean' : 'string',
                  title: field.label, enum: field.options?.map((item: Entity) => item.value), minimum: field.min, maximum: field.max,
                }} presentation={field} value={inputs[control.command]?.[field.key]} disabled={!canManage || action.busy}
                  onChange={value => setInputs(current => ({ ...current, [control.command]: { ...current[control.command], [field.key]: value } }))} />)}
                <button className="btn" type="button" disabled={!canManage || !plugin.enabled || action.busy} onClick={() => { void action.run(async () => {
                  setResult(await api(base + '/actions/' + encodeURIComponent(control.command), { method: 'POST',
                    body: JSON.stringify({ expected_revision: revision, value: inputs[control.command] || {} }) }))
                }) }}>{control.label}</button></div>)}</section>)}</div>
        {result && <pre className="config-preview plugin-action-result">{JSON.stringify(result, null, 2)}</pre>}
        <div className="editor-actions"><button className="btn btn-primary" type="submit" disabled={!canManage || action.busy || !fields.length}>{t('保存配置')}</button></div>
      </form>}
      {tab === 'diagnostics' && <><div className="row-actions"><button className="btn" disabled={action.busy} onClick={() => { void action.run(async () => setDiagnostics(await api(base + '/diagnostics'))) }}>{t('刷新诊断')}</button></div>
        <p className="hint">{t('诊断不会自动发起模型调用。无法查询的额度和用量显示 unavailable。')}</p>
        <pre className="config-preview plugin-diagnostics">{JSON.stringify(diagnostics, null, 2)}</pre></>}
    </div>}
  </>
}
