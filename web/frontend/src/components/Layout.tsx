import type { ReactNode } from 'react'
import { Activity, Boxes, ChartNoAxesCombined, KeyRound, LayoutDashboard, LogOut, RefreshCw, Settings, Sparkles } from 'lucide-react'
import { useAdmin } from '../AdminContext'
import type { PageId } from '../types'

const nav: { id: PageId; label: string; icon: typeof Activity }[] = [
  { id: 'dashboard', label: '仪表盘', icon: LayoutDashboard },
  { id: 'providers', label: '模型厂商', icon: Boxes },
  { id: 'models', label: '模型', icon: Sparkles },
  { id: 'keys', label: 'API 密钥', icon: KeyRound },
  { id: 'logs', label: '统计日志', icon: ChartNoAxesCombined },
  { id: 'settings', label: '系统设置', icon: Settings },
]

export function Sidebar({ page, onPage }: { page: PageId; onPage: (page: PageId) => void }) {
  return <aside className="sidebar">
    <div className="brand"><img className="brand-logo" src="/admin/logo.png" alt="Kemo Gateway Logo"/><div className="brand-copy"><strong>kemo gateway</strong></div></div>
    <span className="nav-label">Navigation</span>
    <nav className="nav-list">{nav.map(item => { const Icon = item.icon; return <button key={item.id} className={page === item.id ? 'active' : ''} onClick={() => onPage(item.id)}><Icon size={17}/><span>{item.label}</span></button> })}</nav>
    <div className="sidebar-watermark">KEMO</div>
  </aside>
}

export function Topbar({ page, children }: { page: PageId; children: ReactNode }) {
  const { data, restart, refreshing, refresh, disconnect, error, clearError } = useAdmin()
  const titles: Record<PageId, [string, string]> = {
    dashboard: ['Kemo Gateway 控制台', '网关实例、Provider、模型和运行时配置的真实状态'],
    providers: ['Provider 控制中心', '已加载厂商包、模型路由和 API 配置'],
    models: ['模型与启停', '根据 Provider 注册结果控制新请求可用模型'],
    keys: ['API 密钥', '当前后端尚未提供安全的密钥元数据与 CRUD 接口'],
    logs: ['统计与日志', '当前后端尚未接入持久化 Usage 与脱敏日志查询'],
    settings: ['系统设置', '热配置、Provider API 配置和 owner 平滑重启'],
  }
  const phase = restart?.gateway.phase ?? data.runtime.phase
  return <main className="main">
    <header className="topbar"><div><h1><b>Kemo</b> {titles[page][0].replace('Kemo ', '')}</h1><p>{titles[page][1]}</p></div><div className="top-actions"><span className={`online phase-${phase}`}><i/>{phase}</span><span className="top-chip">Protocol {data.protocol_version}</span><span className="top-chip">v{data.version}</span>{!data.authentication.required && <span className="top-chip">免登录模式</span>}<button className="btn" disabled={refreshing} onClick={() => void refresh()}><RefreshCw className={refreshing ? 'spin' : ''} size={15}/>刷新</button>{data.authentication.required && <button className="btn" onClick={disconnect}><LogOut size={15}/>退出</button>}</div></header>
    {error && <div className="global-alert" role="alert"><span>{error}</span><button onClick={clearError}>×</button></div>}
    {children}
  </main>
}
