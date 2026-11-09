import { useRef, useState } from 'react'
import { api, useAction, useConsole } from '../console'
import type { Entity } from '../console'
import { Modal, PageHeader, Toggle, VerifyBadge } from '../components/UI'
import { Empty, ErrorBar } from '../components/Form'
import { Select } from '../components/Select'
import { t } from '../i18n'

export default function Extensions({ kind }: { kind: 'protocol_adapters' | 'provider_templates' }) {
  const { data, mutate, refresh } = useConsole()
  const fileInput = useRef<HTMLInputElement>(null)
  const action = useAction()
  const [logs, setLogs] = useState<{ id: string; lines: string[] } | null>(null)
  const [source, setSource] = useState('all')
  const [replace, setReplace] = useState(false)
  const [branch, setBranch] = useState('main')
  const [plan, setPlan] = useState<Entity | null>(null)
  const [updated, setUpdated] = useState(false)
  const adapters = kind === 'protocol_adapters'
  const items = (adapters ? data!.adapters : data!.templates).filter(item => source === 'all' || item.source === source)
  return <div className="page"><PageHeader title={adapters ? '协议适配器' : '提供商模板'}
    actions={<>
      <Select aria-label={t('更新分支')} value={branch} disabled={action.busy}
        onChange={value => { setBranch(value); setPlan(null) }}
        options={[{ value: 'main', label: 'main' }, { value: 'kemo-preview', label: 'kemo-preview' }]} />
      <button className="btn" disabled={action.busy || !data!.permissions.can_manage_extensions} onClick={() => { void action.run(async () => {
        setPlan(await mutate('/resources/check', 'POST', { branch }))
      }) }}>{t('检查更新')}</button>
      <button className="btn btn-primary" disabled={action.busy || !data!.permissions.can_manage_extensions} onClick={() => fileInput.current?.click()}>{t('安装扩展')}</button>
    </>} />
    <ErrorBar error={action.error} />
    <div className="row-actions">
      <Select aria-label={t('来源')} value={source} onChange={setSource}
        options={[{ value: 'all', label: t('全部') }, { value: 'builtin', label: t('内置') }, { value: 'user', label: t('用户') }]} />
      <label className="check-label"><input type="checkbox" checked={replace} onChange={e => setReplace(e.target.checked)} />{t('替换同名用户资源（需先停用）')}</label>
    </div>
    <p className="hint">{t('内置资源随更新覆盖；用户资源独立保存，更新时保留。')}</p>
    {(updated || data!.resources.restart_required) && <p className="hint" role="status">{t('内置资源已更新，请重启网关生效。')}</p>}
    <input hidden type="file" ref={fileInput} accept=".zip" onChange={e => {
      const file = e.target.files?.[0]
      if (file) void action.run(async () => {
        const form = new FormData(); form.set('file', file); form.set('expected_revision', data!.revision)
        form.set('replace', String(replace))
        await api(`/extensions/${kind}/install`, { method: 'POST', body: form }); await refresh()
      })
      e.target.value = ''
    }} />
    <div className="card table-scroll"><table className="model-table"><thead><tr>{['名称', '版本', '操作', '来源', '启用状态'].map(l => <th key={l}>{t(l)}</th>)}</tr></thead><tbody>
      {items.map(item => <tr key={`${item.builtin ? 'builtin' : 'extension'}:${item.id}`}><td><div className="model-id">{item.name || item.id}</div><div className="small muted mono">{item.id}</div></td><td>{item.version || '—'}</td>
        <td>{item.operations?.join(', ') || item.template?.adapter_id || '—'}{!adapters && <div className="small muted">{t(item.category === 'coding_service' ? '编码工具服务' : '模型服务')}</div>}</td><td>{t(item.builtin ? '内置' : '用户')}{!adapters && <VerifyBadge status={item.verification || 'declared'} />}</td>
        <td>{item.builtin ? <span className="tag">{t('已启用')}</span> : <><Toggle checked={!!item.enabled} disabled={action.busy || !item.valid} label={t('启用')} onChange={enabled => { void action.run(() => mutate(`/extensions/${kind}/${item.id}`, 'PATCH', { enabled })) }} />
          {adapters && item.enabled && <button className="btn btn-sm" onClick={() => { void action.run(async () => {
            const result = await api(`/extensions/protocol_adapters/${item.id}/logs`)
            setLogs({ id: item.id, lines: result.lines })
          }) }}>{t('Worker 日志')}</button>}</>}</td></tr>)}
    </tbody></table>{!items.length && <Empty text="暂无扩展" />}</div>
    <Modal open={plan !== null} title={t('内置资源更新')} onClose={() => setPlan(null)} width={680}>
      <p className="hint">{t('仅更新内置适配器和模板，框架版本、用户资源和运行数据保持不变。')}</p>
      <p className="small mono">GitHub · {plan?.branch} · {plan?.target_commit}</p>
      {plan && Object.entries(plan.groups as Record<string, Entity>).map(([key, group]) => <div className="edit-section" key={key}>
        <strong>{t(key === 'protocol_adapters' ? '协议适配器' : '提供商模板')}</strong>
        <p className="mono">{group.current} → {group.target}</p>
        {group.changes.map((item: Entity) => <div key={item.id} className="small mono">{item.id} · {item.current || '—'} → {item.target || '—'}</div>)}
      </div>)}
      {!plan?.available && <p>{t('无需更新')}</p>}
      <ErrorBar error={action.error} />
      <button className="btn btn-primary" disabled={action.busy || !plan?.available} onClick={() => { void action.run(async () => {
        const result = await mutate('/resources/apply', 'POST', { target_commit: plan!.target_commit, resource_revision: plan!.resource_revision })
        setUpdated(result.restart_required); setPlan(null)
      }) }}>{t('更新内置资源')}</button>
    </Modal>
    <Modal open={logs !== null} title={t('Worker 日志')} onClose={() => setLogs(null)} width={900}>
      <p className="hint mono">{logs?.id}</p><pre className="config-editor mono">{logs?.lines.join('\n') || t('暂无日志')}</pre>
    </Modal>
  </div>
}
