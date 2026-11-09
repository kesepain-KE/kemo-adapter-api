import { useRef, useState } from 'react'
import { api, useAction, useConsole } from '../console'
import { Modal, PageHeader, Toggle, VerifyBadge } from '../components/UI'
import { Empty, ErrorBar } from '../components/Form'
import { t } from '../i18n'

export default function Extensions({ kind }: { kind: 'protocol_adapters' | 'provider_templates' }) {
  const { data, mutate, refresh } = useConsole()
  const fileInput = useRef<HTMLInputElement>(null)
  const action = useAction()
  const [logs, setLogs] = useState<{ id: string; lines: string[] } | null>(null)
  const adapters = kind === 'protocol_adapters'
  const items = adapters ? data!.adapters : data!.templates
  return <div className="page"><PageHeader title={adapters ? '协议适配器' : '提供商模板'}
    actions={<button className="btn btn-primary" disabled={action.busy} onClick={() => fileInput.current?.click()}>{t('安装扩展')}</button>} />
    <ErrorBar error={action.error} />
    <input hidden type="file" ref={fileInput} accept=".zip" onChange={e => {
      const file = e.target.files?.[0]
      if (file) void action.run(async () => {
        const form = new FormData(); form.set('file', file); form.set('expected_revision', data!.revision)
        await api(`/extensions/${kind}/install`, { method: 'POST', body: form }); await refresh()
      })
      e.target.value = ''
    }} />
    <div className="card table-scroll"><table className="model-table"><thead><tr>{['名称', '版本', '操作', '来源', '启用状态'].map(l => <th key={l}>{t(l)}</th>)}</tr></thead><tbody>
      {items.map(item => <tr key={`${item.builtin ? 'builtin' : 'extension'}:${item.id}`}><td><div className="model-id">{item.name || item.id}</div><div className="small muted mono">{item.id}</div></td><td>{item.version || '—'}</td>
        <td>{item.operations?.join(', ') || item.template?.adapter_id || '—'}{!adapters && <div className="small muted">{t(item.category === 'coding_service' ? '编码工具服务' : '模型服务')}</div>}</td><td>{t(item.builtin ? '内置' : '扩展')}{!adapters && <VerifyBadge status={item.verification || 'declared'} />}</td>
        <td>{item.builtin ? <span className="tag">{t('已启用')}</span> : <><Toggle checked={!!item.enabled} disabled={action.busy || !item.valid} label={t('启用')} onChange={enabled => { void action.run(() => mutate(`/extensions/${kind}/${item.id}`, 'PATCH', { enabled })) }} />
          {adapters && item.enabled && <button className="btn btn-sm" onClick={() => { void action.run(async () => {
            const result = await api(`/extensions/protocol_adapters/${item.id}/logs`)
            setLogs({ id: item.id, lines: result.lines })
          }) }}>{t('Worker 日志')}</button>}</>}</td></tr>)}
    </tbody></table>{!items.length && <Empty text="暂无扩展" />}</div>
    <Modal open={logs !== null} title={t('Worker 日志')} onClose={() => setLogs(null)} width={900}>
      <p className="hint mono">{logs?.id}</p><pre className="config-editor mono">{logs?.lines.join('\n') || t('暂无日志')}</pre>
    </Modal>
  </div>
}
