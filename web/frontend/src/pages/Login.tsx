import { useEffect, useRef, useState } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { LoaderCircle } from 'lucide-react'
import { ApiError, api, setCsrf, useAction, useConsole } from '../console'
import { ErrorBar } from '../components/Form'
import { LoginLayout } from '../components/LoginLayout'
import { t } from '../i18n'
import TokenLogin from './TokenLogin'
import UserLogin from './UserLogin'

type AuthMethods = {
  token_required: boolean; password_required: boolean; configuration_valid: boolean
  next_step: 'token' | 'password' | 'complete'
}
type AuthSession = { next_step: 'password'; csrf_token: null } | { next_step: 'complete'; csrf_token: string }

export default function Login() {
  const { refresh } = useConsole()
  const location = useLocation()
  const navigate = useNavigate()
  const returnTo = useRef(location.state?.returnTo ?? (location.pathname.startsWith('/auth/') ? '/' : location.pathname + location.search))
  const [methods, setMethods] = useState<AuthMethods | null>(null)
  const [loadError, setLoadError] = useState('')
  const [reload, setReload] = useState(0)
  const action = useAction()
  useEffect(() => {
    const controller = new AbortController()
    setLoadError('')
    void api<AuthMethods>('/auth/methods', { signal: controller.signal }).then(async next => {
      if (controller.signal.aborted) return
      setMethods(next)
      if (next.configuration_valid && next.next_step === 'complete') {
        await refresh()
        navigate(returnTo.current, { replace: true })
      }
    }).catch(cause => { if (!controller.signal.aborted) setLoadError(cause.message) })
    return () => controller.abort()
  }, [refresh, navigate, reload])

  const authenticate = (step: 'token' | 'password', credentials: { token: string } | { username: string; password: string }) => {
    void action.run(async () => {
      let session: AuthSession
      try {
        session = await api<AuthSession>(`/auth/${step}`, { method: 'POST', body: JSON.stringify(credentials) })
      } catch (cause) {
        if (step === 'password' && cause instanceof ApiError && cause.status === 401) {
          setMethods(await api<AuthMethods>('/auth/methods'))
        }
        throw cause
      }
      if (session.next_step === 'password') {
        setMethods(current => ({ ...current!, next_step: 'password' }))
      } else {
        setCsrf(session.csrf_token)
        await refresh()
        navigate(returnTo.current, { replace: true })
      }
    })
  }

  if (!methods || !methods.configuration_valid || methods.next_step === 'complete' || loadError) {
    const error = loadError || (methods && !methods.configuration_valid ? 'WEB_USERNAME 与 WEB_PASSWORD 必须同时配置' : '')
    return <LoginLayout title="登录管理控制台" description="Kemo Gateway">
      {error ? <><ErrorBar error={error} /><button className="btn" onClick={() => setReload(value => value + 1)}>{t('重试')}</button></>
        : <p className="login-status" role="status"><LoaderCircle size={18} className="spin" />{t('正在加载')}</p>}
    </LoginLayout>
  }
  const tokenStep = methods.next_step === 'token'
  const path = tokenStep ? '/auth/token' : '/auth/user'
  const twoSteps = methods.token_required && methods.password_required
  return <Routes>
    <Route path={path} element={tokenStep
      ? <TokenLogin twoSteps={twoSteps} busy={action.busy} error={action.error} onSubmit={token => authenticate('token', { token })} />
      : <UserLogin twoSteps={twoSteps} busy={action.busy} error={action.error} onSubmit={credentials => authenticate('password', credentials)} />} />
    <Route path="*" element={<Navigate to={path} replace state={{ returnTo: returnTo.current }} />} />
  </Routes>
}
