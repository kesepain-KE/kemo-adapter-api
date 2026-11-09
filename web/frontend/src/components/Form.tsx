import { Children, cloneElement, isValidElement, useId } from 'react'
import type { ReactElement, ReactNode } from 'react'
import { t } from '../i18n'
import { Select } from './Select'
import { DateTimePicker } from './DateTimePicker'

export function Field({ label, children, hint, error }: { label: string; children: ReactNode; hint?: string; error?: string }) {
  const id = useId()
  return <div className="field"><label htmlFor={id}>{t(label)}</label>{Children.map(children, child =>
    isValidElement(child) && (['input', 'select', 'textarea'].includes(String(child.type)) || child.type === Select || child.type === DateTimePicker)
      ? cloneElement(child as ReactElement<{ id?: string }>, { id })
      : child
  )}{hint && <span className="hint">{t(hint)}</span>}{error && <span className="hint danger-text" role="alert">{error}</span>}</div>
}
export function ErrorBar({ error }: { error: string }) {
  return error ? <div className="hint-bar error-bar" role="alert">{t(error)}</div> : null
}
export function Empty({ text = '暂无数据' }: { text?: string }) {
  return <div className="empty"><div className="empty-title">{t(text)}</div></div>
}
export function JsonEditor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return <textarea className="input mono config-editor" value={value} spellCheck={false} onChange={e => onChange(e.target.value)} />
}
