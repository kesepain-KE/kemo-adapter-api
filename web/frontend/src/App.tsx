import { useState } from 'react'
import { KeyRound, LoaderCircle, ShieldCheck } from 'lucide-react'
import { AdminProvider, useAdminSession } from './AdminContext'
import { Sidebar, Topbar } from './components/Layout'
import Dashboard from './pages/Dashboard'
import Keys from './pages/Keys'
import Logs from './pages/Logs'
import Models from './pages/Models'
import Providers from './pages/Providers'
import Settings from './pages/Settings'
import type { PageId } from './types'

function Login() {
  const { booting, error, connect } = useAdminSession()
  const [token, setToken] = useState('')
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    void connect(token)
  }
  return <main className="login-screen"><section className="login-card">
    <img className="brand-logo login-logo" src="/admin/logo.png" alt="Kemo Gateway Logo"/>
    <h1>Kemo Gateway</h1>
    <p>连接真实管理 API 后才能查看和修改网关。页面不会使用演示数据。</p>
    <form onSubmit={submit}>
      <label htmlFor="admin-token">管理端密钥</label>
      <div className="login-input"><KeyRound size={18}/><input id="admin-token" type="password" autoComplete="current-password" value={token} onChange={event => setToken(event.target.value)} placeholder="需要 admin:web 或 owner scope" autoFocus/></div>
      {error && <div className="login-error">{error}</div>}
      <button className="button" disabled={booting}>{booting ? <><LoaderCircle className="spin" size={16}/>正在连接</> : <><ShieldCheck size={16}/>连接管理 API</>}</button>
    </form>
    <small>密钥仅保存在当前浏览器会话，后端不会向网页回传任何密钥。</small>
  </section></main>
}

function Console() {
  const { booting, data } = useAdminSession()
  const [page, setPage] = useState<PageId>('dashboard')
  if (booting && !data) return <main className="login-screen"><LoaderCircle className="spin" size={28}/><p>正在恢复管理会话…</p></main>
  if (!data) return <Login/>
  const pages = { dashboard: <Dashboard/>, providers: <Providers onSettings={() => setPage('settings')}/>, models: <Models/>, keys: <Keys/>, logs: <Logs/>, settings: <Settings/> }
  return <div className="app"><Sidebar page={page} onPage={setPage}/><Topbar page={page}>{pages[page]}</Topbar></div>
}

export default function App() {
  return <AdminProvider><Console/></AdminProvider>
}
