import { useEffect, useRef, useState } from 'react'
import { CalendarDays, ChevronDown, ChevronLeft, ChevronRight } from 'lucide-react'

export function localDate(): string {
  const now = new Date()
  const offset = now.getTimezoneOffset() * 60_000
  return new Date(now.getTime() - offset).toISOString().slice(0, 10)
}

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六']

function shiftMonth(month: string, delta: number): string {
  const [year, monthNumber] = month.split('-').map(Number)
  const value = new Date(Date.UTC(year, monthNumber - 1 + delta, 1))
  return `${value.getUTCFullYear()}-${String(value.getUTCMonth() + 1).padStart(2, '0')}`
}

function calendarDays(month: string): Array<{ iso: string; day: number; current: boolean }> {
  const [year, monthNumber] = month.split('-').map(Number)
  const first = new Date(Date.UTC(year, monthNumber - 1, 1))
  const days: Array<{ iso: string; day: number; current: boolean }> = []
  for (let index = 0; index < 42; index += 1) {
    const value = new Date(Date.UTC(year, monthNumber - 1, index - first.getUTCDay() + 1))
    const iso = `${value.getUTCFullYear()}-${String(value.getUTCMonth() + 1).padStart(2, '0')}-${String(value.getUTCDate()).padStart(2, '0')}`
    days.push({ iso, day: value.getUTCDate(), current: value.getUTCMonth() === monthNumber - 1 })
  }
  return days
}

export default function DatePicker({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const [open, setOpen] = useState(false)
  const [month, setMonth] = useState(() => value ? value.slice(0, 7) : localDate().slice(0, 7))
  const root = useRef<HTMLDivElement>(null)
  const today = localDate()

  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('pointerdown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [])

  const choose = (next: string) => {
    onChange(next)
    if (next) setMonth(next.slice(0, 7))
    setOpen(false)
  }

  return <div className="stats-date-picker" ref={root}>
    <button type="button" className={`stats-date-trigger ${open ? 'open' : ''}`} onClick={() => { setMonth(value ? value.slice(0, 7) : today.slice(0, 7)); setOpen(current => !current) }} aria-expanded={open} aria-haspopup="dialog">
      <CalendarDays size={15}/><span>{value ? value.replaceAll('-', '/') : '统计时区今天'}</span><ChevronDown size={14}/>
    </button>
    {open && <div className="stats-date-popover" role="dialog" aria-label="选择统计日期">
      <header><button type="button" className="stats-date-nav" onClick={() => setMonth(current => shiftMonth(current, -1))} aria-label="上个月"><ChevronLeft size={16}/></button><strong>{month.replace('-', '年')}月</strong><button type="button" className="stats-date-nav" onClick={() => setMonth(current => shiftMonth(current, 1))} aria-label="下个月"><ChevronRight size={16}/></button></header>
      <div className="stats-date-weekdays">{WEEKDAYS.map(day => <span key={day}>{day}</span>)}</div>
      <div className="stats-date-grid">{calendarDays(month).map(cell => <button type="button" key={cell.iso} className={`${cell.current ? '' : 'outside '} ${cell.iso === value ? 'selected ' : ''}${cell.iso === today ? 'today' : ''}`} onClick={() => choose(cell.iso)} aria-label={cell.iso}>{cell.day}</button>)}</div>
      <footer><button type="button" onClick={() => choose('')}>清除</button><button type="button" onClick={() => choose(today)}>今天</button></footer>
    </div>}
  </div>
}
