import { useEffect, useState } from 'react'
import { Check, ChevronRight, RefreshCw, RotateCw, Save } from 'lucide-react'
import { api, useAction, useConsole } from '../console'
import type { Entity } from '../console'
import { PageHeader, Toggle, useToast } from '../components/UI'
import { ErrorBar, Field } from '../components/Form'
import { getLanguage, languages, setLanguage, t } from '../i18n'
import { Select } from '../components/Select'

const groups: Record<string, string[][]> = {
  preferences: [['language', '显示语言']],
  gateway: [
    ['runtime', '运行策略'],
    ['startup', '启动配置'],
    ['export', '配置导出'],
  ],
  prompt: [['global', '全局指令']],
  system: [
    ['process', '进程管理'],
    ['version', '版本信息'],
  ],
}

export default function Settings() {
  const { data } = useConsole()
  const [tab, setTab] = useState('preferences')
  const [visited, setVisited] = useState(['preferences'])
  const [selected, setSelected] = useState<Record<string, string>>({
    preferences: 'language', gateway: 'runtime', prompt: 'global', system: 'process',
  })
  const tabs = [['preferences', '界面偏好'], ['gateway', '网关配置'], ['prompt', '最高系统提示词'], ['system', '系统维护']]
  return <div className="page settings-page"><PageHeader title="设置" />
    <div className="page-tabs settings-tabs" role="tablist" aria-label={t('设置栏目')}>
      {tabs.map(([id, label]) =>
        <button key={id} id={`settings-${id}`} role="tab" aria-selected={tab === id} aria-controls={`settings-content-${id}`} onClick={() => {
          setTab(id); setVisited(current => current.includes(id) ? current : [...current, id])
        }}>{t(label)}</button>)}
    </div><div className="settings-workspace">
    <nav className="settings-group-nav" aria-label={t(tabs.find(([id]) => id === tab)![1])}>
      {groups[tab].filter(([id]) => id !== 'startup' || data!.permissions.can_restart).map(([id, label]) =>
        <button key={id} id={`settings-group-${tab}-${id}`} type="button" aria-current={selected[tab] === id ? 'page' : undefined}
          aria-controls={`settings-content-${tab}`} onClick={() => setSelected(current => ({ ...current, [tab]: id }))}>
          <strong>{t(label)}</strong>
          <ChevronRight size={14} />
        </button>)}
    </nav><div className="settings-panels">{visited.map(id => <div key={id} id={`settings-content-${id}`} role="tabpanel" hidden={tab !== id}
      aria-labelledby={`settings-${id}`} className="settings-content">
      {id === 'preferences' ? <Preferences /> : id === 'gateway' ? <GatewaySettings section={selected.gateway} /> : id === 'prompt' ? <SystemPrompt /> : <SystemMaintenance section={selected.system} />}
    </div>)}</div></div>
  </div>
}

function Preferences() {
  return <section className="settings-section"><div className="card settings-section-body">
    <div className="panel-head"><h2 className="panel-title">{t('显示语言')}</h2></div><div className="panel-body preference-row">
    <h3>{t('语言')}</h3>
    <Field label="显示语言">
      <Select className="language-select" value={getLanguage()} onChange={value => setLanguage(value as typeof languages[number]['code'])}
        options={languages.map(language => ({ value: language.code, label: language.name }))} />
    </Field>
  </div></div></section>
}

function GatewaySettings({ section }: { section: string }) {
  const { data, mutate } = useConsole()
  const [settings, setSettings] = useState(data!.settings)
  const [revision, setRevision] = useState(data!.revision)
  const action = useAction()
  const toast = useToast()
  return <>
    <ErrorBar error={action.error} />
    <section className="settings-section" hidden={section !== 'runtime'}><div className="card settings-section-body">
      <div className="panel-head"><h2 className="panel-title">{t('运行策略')}</h2><span className="tag">{t('实时生效')}</span></div><div className="setting-switch-row">
      <h3>{t('对外 API')}</h3>
      <Toggle checked={settings.enabled} onChange={enabled => setSettings({ ...settings, enabled })} label={t('启用')} />
    </div><div className="panel-body form-grid two">
      <Field label="最大尝试次数"><input className="input" type="number" min="1" max="10" value={settings.max_attempts} onChange={e => setSettings({ ...settings, max_attempts: Number(e.target.value) })} /></Field>
      <Field label="冷却时间（秒）"><input className="input" type="number" min="1" max="3600" value={settings.cooldown_seconds} onChange={e => setSettings({ ...settings, cooldown_seconds: Number(e.target.value) })} /></Field>
    </div><div className="panel-foot settings-actions"><button className="btn btn-primary" disabled={action.busy} onClick={() => { void action.run(async () => {
        const { enabled, max_attempts, cooldown_seconds } = settings
        const result = await mutate('/settings', 'PATCH', { enabled, max_attempts, cooldown_seconds }, false, revision)
        setRevision(result.revision); toast(t('配置已保存'))
      }) }}><Save size={14} />{t('保存运行策略')}</button>
    </div></div></section>
    {data!.permissions.can_restart && <StartupConfig hidden={section !== 'startup'} />}
    <section className="settings-section" hidden={section !== 'export'}><div className="card settings-section-body">
      <div className="panel-head"><h2 className="panel-title">{t('配置导出')}</h2></div><div className="panel-body settings-export">
      <span className="mono">catalog.yaml</span><a className="btn" href="/admin/api/catalog.yaml" download>{t('导出脱敏 YAML 配置')}</a>
    </div></div></section>
  </>
}

function StartupConfig({ hidden }: { hidden: boolean }) {
  const [document, setDocument] = useState<Entity | null>(null)
  const [yaml, setYaml] = useState('')
  const [secrets, setSecrets] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [running, setRunning] = useState<Entity | null>(null)
  const [reload, setReload] = useState(0)
  const action = useAction()
  useEffect(() => {
    const controller = new AbortController()
    setError('')
    Promise.all([api('/system/startup-config', { signal: controller.signal }), api('/system/gateway-config', { signal: controller.signal })])
      .then(([next, current]) => { setDocument(next); setYaml(next.yaml); setSecrets({}); setRunning(current); setSaved(false) })
      .catch(cause => { if (!controller.signal.aborted) setError(cause.message) })
    return () => controller.abort()
  }, [reload])
  return <section className="settings-section" hidden={hidden}><div className="card settings-section-body"><div className="panel-head"><h2 className="panel-title mono">gateway.yaml</h2>
    <div className="row-actions"><span className="tag">{t('重启生效')}</span>
      <button className="btn btn-sm" disabled={action.busy} onClick={() => {
        if (window.confirm(t('重新加载将丢弃尚未保存的启动配置，是否继续？'))) {
          action.clearError(); setReload(value => value + 1)
        }
      }}><RefreshCw size={13} />{t('重新加载')}</button></div></div>
    <div className="panel-body startup-editor"><ErrorBar error={error || action.error} />
      {document && <>
        {!!document.environment_overrides.length && <p className="hint-bar">{t('以下配置由进程环境覆盖')}：<code>{document.environment_overrides.join(', ')}</code></p>}
        {!!document.hidden_fields.length && <p className="hint-bar">{t('含敏感信息的 Base URL 已隐藏；如需修改，请在 YAML 中填写不含凭据的地址。')}</p>}
        <Field label="启动配置 YAML"><textarea className="input mono config-editor startup-yaml" spellCheck={false} value={yaml} onChange={event => { setYaml(event.target.value); setSaved(false) }} /></Field>
        <details className="startup-secrets"><summary>{t('认证与敏感配置')}</summary><p className="hint">{t('勾选后才替换该项；留空并保存表示清除。密钥原文不会回显。')}</p>
          <div className="form-grid two">{document.secrets.map((item: Entity) => {
            const replacing = Object.hasOwn(secrets, item.name)
            return <div className="field" key={item.name}><label className="check-label">
              <input type="checkbox" checked={replacing} onChange={event => setSecrets(current => {
                const next = { ...current }; if (event.target.checked) next[item.name] = ''; else delete next[item.name]; return next
              })} />{t(item.label)} <code className="muted">{item.preview}</code></label>
              <input className="input mono" type="password" autoComplete="new-password" aria-label={`${t('替换')} ${item.name}`}
                disabled={!replacing} value={secrets[item.name] || ''} placeholder={t('保持原值')} onChange={event => {
                  setSecrets({ ...secrets, [item.name]: event.target.value }); setSaved(false)
                }} /></div>
          })}</div></details>
        {saved && <p className="hint-bar success-text"><Check size={15} />{t('启动配置已保存，请到系统维护重启网关。')}</p>}
      </>}
      {running && <details className="running-config"><summary>{t('查看当前进程配置')}</summary>
        {running.groups.map((group: Entity) => <div key={group.title}><h3>{t(group.title)}</h3>{group.items.map((item: Entity) =>
          <div className="list-row" key={item.name}><code>{item.name}</code><span className="mono">{item.value || '—'}</span></div>)}</div>)}
      </details>}
    </div>
      {document && <div className="panel-foot settings-actions"><button className="btn btn-primary" disabled={action.busy} onClick={() => { void action.run(async () => {
          const next = await api('/system/startup-config', { method: 'PUT', body: JSON.stringify({
            expected_revision: document.expected_revision, yaml, secrets,
          }) })
          setDocument(next); setYaml(next.yaml); setSecrets({}); setSaved(true)
        }) }}><Save size={14} />{t('保存启动配置')}</button></div>}
    </div>
  </section>
}

function SystemPrompt() {
  const { data, mutate } = useConsole()
  const [prompt, setPrompt] = useState(data!.settings.system_prompt)
  const [revision, setRevision] = useState(data!.revision)
  const action = useAction()
  const toast = useToast()
  return <section className="settings-section"><div className="card settings-section-body">
    <div className="panel-head"><h2 className="panel-title">{t('全局指令')}</h2><span className="tag">{t('实时生效')}</span></div>
    <div className="panel-body"><ErrorBar error={action.error} />
      <Field label="最高优先级系统提示词" hint="统一应用于网关转发的模型请求；留空表示不追加。">
        <textarea className="input config-editor system-prompt-editor" maxLength={100000} value={prompt} onChange={event => setPrompt(event.target.value)} />
      </Field></div><div className="panel-foot settings-actions"><span className="hint mono">{prompt.length.toLocaleString(getLanguage())} / 100,000</span><button className="btn btn-primary" disabled={action.busy} onClick={() => { void action.run(async () => {
        const result = await mutate('/settings', 'PATCH', { system_prompt: prompt }, false, revision)
        setRevision(result.revision); toast(t('配置已保存'))
      }) }}><Save size={14} />{t('保存系统提示词')}</button>
    </div>
    </div>
  </section>
}

function SystemMaintenance({ section }: { section: string }) {
  const { data, refresh } = useConsole()
  const [status, setStatus] = useState<Entity | null>(null)
  const [pending, setPending] = useState<Entity | null>(null)
  const [version, setVersion] = useState<Entity | null>(null)
  const [requestId, setRequestId] = useState('')
  const [error, setError] = useState('')
  const action = useAction()
  const load = async () => {
    const [current, changes] = await Promise.all([api('/system/restart'), api('/system/restart-required')])
    setStatus(current); setPending(changes); setError('')
    return current
  }
  useEffect(() => {
    if (!data!.permissions.can_restart) return
    void load().catch(cause => setError(cause.message))
  }, [])
  useEffect(() => {
    if (!requestId) return
    let stopped = false
    const timer = window.setInterval(() => {
      void load().then(current => {
        if (stopped) return
        if (current.restart.request_id === requestId && ['succeeded', 'failed'].includes(current.restart.phase)) {
          setRequestId(''); void refresh().catch(() => {})
        }
      }).catch(cause => { if (!stopped) setError(`${t('等待网关重新连接')} · ${cause.message}`) })
    }, 1500)
    return () => { stopped = true; window.clearInterval(timer) }
  }, [requestId])
  const phase = status?.gateway?.phase || data!.runtime.phase
  const versionLabels: Record<string, string> = {
    unavailable: '版本源暂不可用', update_available: '有可用更新', up_to_date: '当前已是最新版本', local_newer: '当前本地版本高于远程版本',
  }
  return <>
    <ErrorBar error={action.error || error} />
    <section className="settings-section" hidden={section !== 'process'}><div className="card settings-section-body"><div className="panel-head"><h2 className="panel-title">{t('启动与重启')}</h2><span className={`tag${phase === 'running' ? ' green' : ''}`}>{phase}</span></div>
      <div className="panel-body maintenance-body"><dl className="maintenance-status">
        <div><dt>{t('启动时间')}</dt><dd>{new Date(data!.runtime.started_at).toLocaleString(getLanguage())}</dd></div>
        <div><dt>{t('进行中')}</dt><dd>{status?.gateway?.active_executions ?? data!.runtime.active_executions}</dd></div>
      </dl><div className="startup-command"><span>{t('启动命令')}</span><code>python start_web.py</code></div>
        <p className="hint">{t('服务运行时可在此优雅重启；停止后需在源码目录执行启动命令。')}</p>
        {pending && <p className={`hint-bar${pending.required ? '' : ' success-text'}`}>{t(pending.required ? '检测到待重启的配置或源码变更' : '当前没有检测到需要重启的改动')}</p>}
        {status?.restart?.phase && status.restart.phase !== 'idle' && <p className="hint">{t('重启状态')} · {status.restart.phase}</p>}
      </div><div className="panel-foot settings-actions"><button className="btn" disabled={action.busy || !!requestId || !data!.permissions.can_restart} onClick={() => { void action.run(load) }}><RefreshCw size={14} />{t('刷新状态')}</button>
          <button className="btn btn-primary" disabled={action.busy || !!requestId || !data!.permissions.can_restart} onClick={() => {
            if (window.confirm(t('确认重启网关？将等待进行中的请求结束，然后加载已保存的配置。'))) void action.run(async () => {
              const result = await api('/system/restart', { method: 'POST', body: JSON.stringify({ force: false, reason: 'Web console requested a graceful restart' }) })
              setRequestId(result.request_id)
            })
          }}><RotateCw size={14} />{t(requestId ? '重启中' : '重启网关')}</button></div>
      </div></section>
    <section className="settings-section" hidden={section !== 'version'}><div className="card settings-section-body"><div className="panel-head"><h2 className="panel-title">{t('版本检测')}</h2></div>
      <div className="panel-body maintenance-body"><dl className="maintenance-status">
        <div><dt>{t('网关版本')}</dt><dd className="mono">v{data!.version.version}</dd></div>
        <div><dt>Kemo Protocol</dt><dd className="mono">2.0</dd></div>
      </dl>
        {version && <><div className="list-row"><span>{t('远程版本')}</span><strong>{version.remote?.version || '—'}</strong></div>
          <p className="hint-bar">{t(versionLabels[version.status] || '未知')}</p></>}
      </div><div className="panel-foot settings-actions"><button className="btn" disabled={action.busy} onClick={() => { void action.run(async () => {
          setVersion(await api('/system/version-check'))
        }) }}><RefreshCw size={14} />{t('检查更新')}</button></div>
      </div></section>
  </>
}
