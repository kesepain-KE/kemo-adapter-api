import { useEffect, useRef, useState } from 'react'
import { Calendar, ChevronLeft, ChevronRight } from 'lucide-react'
import { getLanguage, t } from '../i18n'
import { Select } from './Select'

const pad = (value: number) => String(value).padStart(2, '0')
const localValue = (date: Date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
const datePart = (date: Date) => localValue(date).slice(0, 10)

export function DateTimePicker({ value, onChange, id, min, max }: {
  value: string; onChange: (value: string) => void; id?: string; min?: string; max?: string
}) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(localValue(new Date()))
  const [month, setMonth] = useState(new Date())
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const language = getLanguage()
  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node) && !(event.target as Element).closest('.select-menu')) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() }
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [open])
  const close = () => { setOpen(false); trigger.current?.focus() }
  const selected = new Date(draft)
  const start = new Date(month.getFullYear(), month.getMonth(), 1)
  start.setDate(start.getDate() - (start.getDay() + 6) % 7)
  const days = Array.from({ length: 42 }, (_, index) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + index))
  const changeTime = (index: number, value: string) => {
    const parts = draft.slice(11).split(':')
    parts[index] = value
    setDraft(`${draft.slice(0, 10)}T${parts.join(':')}`)
  }
  const invalid = !!(min && draft < min || max && draft > max)
  return <div className="datetime-picker" ref={root}>
    <button ref={trigger} id={id} type="button" className="input datetime-trigger" aria-haspopup="dialog" aria-expanded={open}
      onClick={() => {
        if (open) { close(); return }
        const next = value ? new Date(value) : new Date()
        setDraft(localValue(next)); setMonth(next); setOpen(true)
      }}>
      <span className={value ? '' : 'muted'}>{value ? new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'medium', hour12: false }).format(new Date(value)) : t('选择日期和时间')}</span>
      <Calendar size={15} />
    </button>
    {open && <div className="datetime-popover" role="dialog" aria-label={t('选择日期和时间')}>
      <div className="calendar-head">
        <button type="button" className="icon-btn" aria-label={t('上个月')} onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}><ChevronLeft size={16} /></button>
        <Select value={String(month.getMonth())} aria-label={t('月份')} onChange={value => setMonth(new Date(month.getFullYear(), Number(value), 1))}
          options={Array.from({ length: 12 }, (_, index) => ({ value: String(index), label: new Intl.DateTimeFormat(language, { month: 'long' }).format(new Date(2024, index, 1)) }))} />
        <input className="input calendar-year" type="number" min="1" max="9999" aria-label={t('年份')} value={month.getFullYear()}
          onChange={event => { const year = Number(event.target.value); if (year >= 1 && year <= 9999) setMonth(new Date(year, month.getMonth(), 1)) }} />
        <button type="button" className="icon-btn" aria-label={t('下个月')} onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}><ChevronRight size={16} /></button>
      </div>
      <div className="calendar-weekdays">{Array.from({ length: 7 }, (_, index) => <span key={index}>
        {new Intl.DateTimeFormat(language, { weekday: 'narrow' }).format(new Date(2024, 0, index + 1))}
      </span>)}</div>
      <div className="calendar-days">{days.map(date => {
        const day = datePart(date)
        const disabled = !!(min && `${day}T23:59:59` < min || max && `${day}T00:00:00` > max)
        return <button key={day} type="button" disabled={disabled} aria-label={day} aria-pressed={day === draft.slice(0, 10)}
          className={`calendar-day${date.getMonth() !== month.getMonth() ? ' outside' : ''}${day === draft.slice(0, 10) ? ' selected' : ''}${day === datePart(new Date()) ? ' today' : ''}`}
          onClick={() => setDraft(`${day}T${draft.slice(11)}`)}>{date.getDate()}</button>
      })}</div>
      <div className="datetime-time">{['时', '分', '秒'].map((label, index) => <div key={label}><span>{t(label)}</span>
        <Select aria-label={t(label)} value={draft.slice(11).split(':')[index]} onChange={value => changeTime(index, value)}
          options={Array.from({ length: index === 0 ? 24 : 60 }, (_, number) => ({ value: pad(number), label: pad(number) }))} />
      </div>)}</div>
      {invalid && <p className="hint danger-text">{t('时间超出所选范围')}</p>}
      <div className="datetime-actions">
        <button type="button" className="btn btn-sm" onClick={() => { onChange(''); close() }}>{t('清除')}</button>
        <button type="button" className="btn btn-sm" onClick={() => { const now = new Date(); setDraft(localValue(now)); setMonth(now) }}>{t('现在')}</button>
        <button type="button" className="btn btn-sm btn-primary" disabled={invalid || Number.isNaN(selected.getTime())} onClick={() => { onChange(draft); close() }}>{t('确定')}</button>
      </div>
    </div>}
  </div>
}
