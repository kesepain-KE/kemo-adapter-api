import { useEffect, useRef, useState } from 'react'
import { RotateCw } from 'lucide-react'
import { api } from '../console'
import type { Entity } from '../console'
import { ErrorBar, Field } from './Form'
import { Select } from './Select'
import { t, getLanguage } from '../i18n'

export function PluginField({ name, schema, value, onChange, disabled, secretSet, presentation = {} }: {
  name: string; schema: Entity; value: any; onChange: (value: any) => void; disabled: boolean; secretSet?: boolean; presentation?: Entity
}) {
  const current = value ?? schema.default ?? presentation.default
  return <Field label={presentation.label || schema.title || name} hint={schema.description}>
    {schema.enum ? <Select value={String(current ?? '')} onChange={selected => onChange(schema.enum.find((option: any) => String(option) === selected))} disabled={disabled}
      options={schema.enum.map((option: any) => ({ value: String(option),
        label: presentation.options?.find((item: Entity) => item.value === option)?.label || String(option) }))} />
      : schema.type === 'boolean' ? <label className="check-label"><input type="checkbox" checked={!!current}
        disabled={disabled} onChange={event => onChange(event.target.checked)} />{t('启用')}</label>
        : schema.type === 'number' || schema.type === 'integer' ? <input className="input" type="number"
          value={current ?? ''} min={schema.minimum} max={schema.maximum} step={schema.type === 'integer' ? 1 : 'any'}
          disabled={disabled} onChange={event => onChange(event.target.value === '' ? undefined : Number(event.target.value))} />
          : ['object', 'array'].includes(schema.type) ? <textarea className="input config-editor mono"
            disabled={disabled} value={typeof value === 'string' ? value : JSON.stringify(value ?? schema.default ?? (schema.type === 'array' ? [] : {}), null, 2)}
            onChange={event => onChange(event.target.value)} />
            : presentation.type === 'text' && !schema.writeOnly ? <textarea className="input" value={current ?? ''}
              disabled={disabled} maxLength={schema.maxLength} onChange={event => onChange(event.target.value)} />
            : <input className="input" type={schema.writeOnly || presentation.masked ? 'password' : 'text'} autoComplete="off"
              value={current ?? ''} disabled={disabled} maxLength={schema.maxLength}
              placeholder={secretSet ? t('已设置，留空保留') : undefined} onChange={event => onChange(event.target.value)} />}
  </Field>
}

export function PluginFrame({ plugin, entry, revision, canManage, onChanged }: {
  plugin: Entity; entry: string; revision: string; canManage: boolean; onChanged: () => Promise<void>
}) {
  const iframe = useRef<HTMLIFrameElement>(null)
  const channel = useRef<MessageChannel | null>(null)
  const frameNonce = useRef('')
  const timer = useRef<number | undefined>(undefined)
  const current = useRef({ revision, canManage, onChanged })
  current.current = { revision, canManage, onChanged }
  const [error, setError] = useState('')
  const [source, setSource] = useState('')
  const [height, setHeight] = useState(530)
  const [attempt, setAttempt] = useState(0)
  const base = `/advanced-plugins/${encodeURIComponent(plugin.id)}`
  useEffect(() => {
    let active = true
    setSource('')
    void api(base + '/ui/manifest').then(value => {
      if (active) setSource(value.asset_base + entry.split('/').map(encodeURIComponent).join('/'))
    }).catch(cause => { if (active) setError(cause.message) })
    return () => { active = false }
  }, [base, plugin.sha256, entry, attempt])
  useEffect(() => () => { channel.current?.port1.close(); channel.current?.port2.close(); window.clearTimeout(timer.current) }, [])
  useEffect(() => {
    channel.current?.port1.postMessage({ type: 'plugin-context-changed', nonce: frameNonce.current, status: plugin.status, revision })
  }, [plugin.status, revision])
  const connect = () => {
    channel.current?.port1.close(); channel.current?.port2.close()
    window.clearTimeout(timer.current); setError('')
    const next = new MessageChannel()
    channel.current = next
    const nonce = crypto.randomUUID()
    frameNonce.current = nonce
    let active = 0
    next.port1.onmessage = async event => {
      const value = event.data
      if (!value || value.version !== 1 || value.nonce !== nonce || !Number.isSafeInteger(value.id)) return
      window.clearTimeout(timer.current)
      if (++active > 8) { active--; next.port1.postMessage({ id: value.id, nonce, error: t('插件请求过多') }); return }
      try {
        const params = value.params || {}
        let result: any
        const mutation = async (suffix: string, body: Entity, method = 'POST') => {
          if (!current.current.canManage) throw new Error(t('需要 owner 权限'))
          const response = await api(base + suffix, { method, body: JSON.stringify({ expected_revision: current.current.revision, value: body }) })
          await current.current.onChanged()
          return response
        }
        switch (value.method) {
          case 'plugin.getContext':
            result = { plugin_id: plugin.id, version: plugin.version, language: getLanguage(),
              theme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light', pages: plugin.ui.pages, status: plugin.status }
            break
          case 'plugin.config.read': result = await api(base + '/config'); break
          case 'plugin.config.update': await mutation('/config', params, 'PATCH'); result = await api(base + '/config'); break
          case 'plugin.model.list': result = await api(base + '/models'); break
          case 'plugin.model.probe': result = await mutation('/probe', params); break
          case 'plugin.account.status': result = await api(base + '/diagnostics'); break
          case 'plugin.account.action':
          case 'plugin.action.invoke':
            if (typeof params.action !== 'string' || !Object.hasOwn(plugin.actions, params.action)) throw new Error(t('插件动作未声明'))
            result = await mutation('/actions/' + encodeURIComponent(params.action), params.value || {}); break
          case 'plugin.ui.resize':
            if (!Number.isFinite(params.height)) throw new Error(t('插件页面高度无效'))
            const nextHeight = Math.max(320, Math.min(2000, Math.ceil(params.height)))
            setHeight(nextHeight); result = { height: nextHeight }; break
          default: throw new Error(t('插件 Bridge 方法不受支持'))
        }
        next.port1.postMessage({ id: value.id, nonce, result })
      } catch (cause) { next.port1.postMessage({ id: value.id, nonce, error: (cause as Error).message }) }
      finally { active-- }
    }
    // The sandbox has an opaque origin. Only this iframe receives the transferred
    // port; subsequent messages are bound to that port and a per-load nonce.
    iframe.current?.contentWindow?.postMessage({ type: 'kemo-plugin-init', version: 1, nonce }, '*', [next.port2])
    timer.current = window.setTimeout(() => setError(t('插件页面未连接，可重试或切换通用配置。')), 10000)
  }
  return <div className="plugin-frame-wrap">
    {error && <div className="plugin-frame-error"><ErrorBar error={error} /><button className="btn" onClick={() => {
      setError(''); setAttempt(value => value + 1)
    }}><RotateCw size={14} />{t('重新加载')}</button></div>}
    {source && <iframe key={`${plugin.sha256}:${entry}:${attempt}`} ref={iframe} title={plugin.name}
      sandbox="allow-scripts" referrerPolicy="no-referrer" onLoad={connect}
      onError={() => setError(t('插件页面加载失败，可切换通用配置。'))}
      src={source} style={{ height, minHeight: height }} />}
  </div>
}
