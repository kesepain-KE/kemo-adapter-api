import { useEffect, useState } from 'react'
import { KeyRound, LoaderCircle, LockKeyhole, ShieldCheck, UserRound } from 'lucide-react'
import { AdminProvider, useAdminSession } from './AdminContext'
import { adminApi, type WebAuthMethods } from './adminApi'
import { Sidebar, Topbar } from './components/Layout'
import Dashboard from './pages/Dashboard'
import Keys from './pages/Keys'
import CallLogs from './pages/CallLogs'
import Logs from './pages/Logs'
import Models from './pages/Models'
import Providers from './pages/Providers'
import Settings from './pages/Settings'
import type { PageId } from './types'

const PAGE_IDS: readonly PageId[] = ['dashboard', 'providers', 'models', 'keys', 'logs', 'call-logs', 'settings']

function pageFromLocation(): PageId {
  const segments = window.location.pathname.split('/').filter(Boolean)
  const adminIndex = segments.lastIndexOf('admin')
  const candidate = adminIndex >= 0 ? segments[adminIndex + 1] : undefined
  return candidate && PAGE_IDS.includes(candidate as PageId) ? candidate as PageId : 'dashboard'
}

function pagePath(page: PageId): string {
  return page === 'dashboard' ? '/admin' : `/admin/${page}`
}

function Login() {
  const { booting, error, connect } = useAdminSession()
  const [token, setToken] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [methods, setMethods] = useState<WebAuthMethods | null>(null)
  const [phase, setPhase] = useState<'loading' | 'token' | 'password'>('loading')
  const [preauthToken, setPreauthToken] = useState('')
  const [authBusy, setAuthBusy] = useState(false)
  const [authError, setAuthError] = useState('')

  const exchangeToken = async (candidate: string) => {
    if (!candidate.trim()) {
      setAuthError('URL 或输入框中的 Token 不能为空。')
      setPhase('token')
      return
    }
    setAuthBusy(true)
    setAuthError('')
    try {
      const session = await adminApi.authenticateWebToken(candidate.trim())
      if (session.next_step === 'password') {
        setPreauthToken(session.session_token)
        setPhase('password')
      } else {
        await connect(session.session_token)
      }
    } catch (reason) {
      setAuthError(reason instanceof Error ? reason.message : 'Web Token 鉴权失败')
      setPhase('token')
    } finally {
      setAuthBusy(false)
    }
  }

  useEffect(() => {
    let active = true
    const initialize = async () => {
      try {
        const value = await adminApi.webAuthMethods()
        if (!active) return
        setMethods(value)
        if (!value.configuration_valid) {
          setAuthError('WEB_USERNAME 与 WEB_PASSWORD 必须同时配置。')
          setPhase('password')
          return
        }
        const parameters = new URLSearchParams(window.location.search)
        const urlToken = parameters.get('token')
        if (urlToken !== null) {
          window.history.replaceState(null, '', `${window.location.pathname}${window.location.hash}`)
          await exchangeToken(urlToken)
        } else if (value.token_required) {
          setPhase('token')
        } else if (value.password_required) {
          setPhase('password')
        } else {
          await connect('', true)
        }
      } catch (reason) {
        if (!active) return
        setAuthError(reason instanceof Error ? reason.message : '无法读取网页登录配置')
        setPhase('token')
      }
    }
    void initialize()
    return () => { active = false }
    // 登录页只在未建立管理会话时挂载一次。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const submitToken = (event: React.FormEvent) => {
    event.preventDefault()
    void exchangeToken(token)
  }

  const submitPassword = async (event: React.FormEvent) => {
    event.preventDefault()
    setAuthBusy(true)
    setAuthError('')
    try {
      const session = await adminApi.authenticateWebPassword(username, password, preauthToken)
      await connect(session.session_token)
    } catch (reason) {
      setAuthError(reason instanceof Error ? reason.message : '用户名或密码鉴权失败')
    } finally {
      setAuthBusy(false)
    }
  }
  return <main className="login-screen"><section className="login-card">
    <img className="brand-logo login-logo" src="/admin/logo.png" alt="Kemo Gateway Logo"/>
    <h1>Kemo Gateway</h1>
    <p>{phase === 'password' && methods?.token_required ? 'Token 验证通过，请继续验证用户名和密码。' : '完成管理身份验证后才能进入网关。'}</p>
    {phase === 'loading' ? <div className="login-auth-loading"><LoaderCircle className="spin" size={20}/>正在读取鉴权方式…</div> : phase === 'token' ? <form onSubmit={submitToken}>
      <label htmlFor="admin-token">Web Token</label>
      <div className="login-input"><KeyRound size={18}/><input id="admin-token" type="password" autoComplete="current-password" value={token} onChange={event => setToken(event.target.value)} placeholder="输入 WEB_TOKEN" autoFocus/></div>
      {(authError || error) && <div className="login-error">{authError || error}</div>}
      <button className="button" disabled={booting || authBusy}>{booting || authBusy ? <><LoaderCircle className="spin" size={16}/>正在验证</> : <><ShieldCheck size={16}/>验证 Token</>}</button>
    </form> : <form onSubmit={submitPassword}>
      <label htmlFor="web-username">用户名</label>
      <div className="login-input"><UserRound size={18}/><input id="web-username" autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} placeholder="WEB_USERNAME" autoFocus/></div>
      <label htmlFor="web-password">密码</label>
      <div className="login-input"><LockKeyhole size={18}/><input id="web-password" type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} placeholder="WEB_PASSWORD"/></div>
      {(authError || error) && <div className="login-error">{authError || error}</div>}
      <button className="button" disabled={booting || authBusy}>{booting || authBusy ? <><LoaderCircle className="spin" size={16}/>正在验证</> : <><ShieldCheck size={16}/>登录控制台</>}</button>
    </form>}
    <small>{phase === 'token' ? `Token 入口：${window.location.origin}?token=<WEB_TOKEN>` : 'Token 会话和登录会话的最长有效期均为 2 小时。'}</small>
  </section></main>
}

function Console() {
  const { booting, data } = useAdminSession()
  const [page, setPage] = useState<PageId>(pageFromLocation)

  useEffect(() => {
    const restorePage = () => setPage(pageFromLocation())
    window.addEventListener('popstate', restorePage)
    return () => window.removeEventListener('popstate', restorePage)
  }, [])

  const navigatePage = (nextPage: PageId) => {
    setPage(nextPage)
    const target = pagePath(nextPage)
    if (window.location.pathname !== target) {
      window.history.pushState({ page: nextPage }, '', `${target}${window.location.search}${window.location.hash}`)
    }
  }

  if (booting && !data) return <main className="login-screen"><LoaderCircle className="spin" size={28}/><p>正在恢复管理会话…</p></main>
  if (!data) return <Login/>
  const pages = { dashboard: <Dashboard/>, providers: <Providers onSettings={() => navigatePage('settings')}/>, models: <Models/>, keys: <Keys/>, logs: <Logs/>, 'call-logs': <CallLogs/>, settings: <Settings/> }
  return <div className="app"><Sidebar page={page} onPage={navigatePage}/><Topbar page={page}>{pages[page]}</Topbar></div>
}

export default function App() {
  return <AdminProvider><Console/></AdminProvider>
}
