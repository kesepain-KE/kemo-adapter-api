import { useState } from 'react'
import { ArrowRight, LoaderCircle } from 'lucide-react'
import { ErrorBar, Field } from '../components/Form'
import { LoginLayout } from '../components/LoginLayout'
import { t } from '../i18n'

export default function UserLogin({ twoSteps, busy, error, onSubmit }: {
  twoSteps: boolean; busy: boolean; error: string; onSubmit: (credentials: { username: string; password: string }) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  return <LoginLayout title="用户认证" description="输入用户名和密码，登录管理控制台。" step="password" twoSteps={twoSteps}>
    <form className="login-form" aria-label={t('用户认证')} aria-busy={busy} onSubmit={event => {
      event.preventDefault()
      if (!busy) onSubmit({ username, password })
    }}>
      <ErrorBar error={error} />
      <Field label="用户名"><input className="input" name="username" required maxLength={512} autoComplete="username"
        autoCapitalize="none" spellCheck={false} autoFocus disabled={busy}
        value={username} onChange={event => setUsername(event.target.value)} /></Field>
      <Field label="密码"><input className="input" type="password" name="password" required maxLength={4096}
        autoComplete="current-password" disabled={busy} value={password} onChange={event => setPassword(event.target.value)} /></Field>
      <button className="btn btn-primary login-submit" type="submit" disabled={busy}>
        {busy ? <><LoaderCircle size={16} className="spin" />{t('正在验证')}</>
          : <>{t('登录')}<ArrowRight size={16} /></>}
      </button>
    </form>
  </LoginLayout>
}
