import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { Check, ChevronDown, Clock3 } from 'lucide-react'
import type { HourlyStatistics } from '../adminApi'

const HOURS = Array.from({ length: 24 }, (_, hour) => hour)

function hourLabel(hour: number): string {
  const start = String(hour).padStart(2, '0')
  return `${start}:00–${start}:59`
}

export default function HourPicker({
  value,
  onChange,
  statistics,
}: {
  value: number | null
  onChange: (value: number | null) => void
  statistics?: HourlyStatistics['items']
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('pointerdown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [])

  const choose = (hour: number | null) => {
    onChange(hour)
    setOpen(false)
  }

  return <div className="stats-hour-picker" ref={root}>
    <button
      type="button"
      className={`stats-hour-trigger ${open ? 'open' : ''}`}
      onClick={() => setOpen(current => !current)}
      aria-expanded={open}
      aria-haspopup="listbox"
    >
      <Clock3 size={15}/>
      <span>{value === null ? '时间段 · 全部' : hourLabel(value)}</span>
      <ChevronDown size={14}/>
    </button>
    {open && <div className="stats-hour-popover" role="listbox" aria-label="选择调用时间段">
      <header><strong>时间段</strong><span>统计时区 · 24 小时</span></header>
      <button
        type="button"
        className={`stats-hour-all ${value === null ? 'selected' : ''}`}
        onClick={() => choose(null)}
        role="option"
        aria-selected={value === null}
      >
        <span>全天</span>{value === null && <Check size={14}/>}
      </button>
      <div className="stats-hour-grid">
        {HOURS.map(hour => {
          const item = statistics?.[hour]
          const calls = item?.calls ?? 0
          const successes = item?.successes ?? 0
          const failures = item?.failures ?? 0
          // 总调用数只负责显示；颜色严格由终态结果贡献，避免失败调用
          // 同时被算进绿色调用量。成功与失败分别形成两层渐变，混合时
          // 可以直接看出该小时的吞吐量与失败压力。
          const successIntensity = Math.min(Math.max(successes, 0) / 648, 1)
          const failureIntensity = Math.min(Math.max(failures, 0) / 648, 1)
          const style = {
            '--hour-success': String(successIntensity),
            '--hour-failure': String(failureIntensity),
          } as CSSProperties
          const details = `${calls} 次调用，${successes} 次成功，${failures} 次失败`
          return <button
            type="button"
            key={hour}
            className={value === hour ? 'selected' : ''}
            style={style}
            onClick={() => choose(hour)}
            role="option"
            aria-selected={value === hour}
            title={details}
          >
            <span>{String(hour).padStart(2, '0')}:00</span>
            <small>{String(hour).padStart(2, '0')}:59</small>
            <em>{calls ? `${calls} 次` : '—'}</em>
          </button>
        })}
      </div>
      <footer className="stats-hour-legend"><span><i className="hour-legend-green"/>成功调用（648 次封顶）</span><span><i className="hour-legend-red"/>失败调用（648 次封顶）</span></footer>
    </div>}
  </div>
}
