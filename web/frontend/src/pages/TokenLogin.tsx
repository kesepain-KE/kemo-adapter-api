import { useState } from 'react'
import { ArrowRight, LoaderCircle } from 'lucide-react'
import { ErrorBar, Field } from '../components/Form'
import { LoginLayout } from '../components/LoginLayout'
import { t } from '../i18n'

export default function TokenLogin({ twoSteps, busy, error, onSubmit }: {
  twoSteps: boolean; busy: boolean; error: string; onSubmit: (token: string) => void
}) {
  const [token, setToken] = useState('')
  return <LoginLayout title="Token 认证" description="输入 Web Token，验证网关访问权限。" step="token" twoSteps={twoSteps}>
    <form className="login-form" aria-label={t('Token 认证')} aria-busy={busy} onSubmit={event => {
      event.preventDefault()
      if (!busy) onSubmit(token)
    }}>
      <ErrorBar error={error} />
      <Field label="Web Token"><input className="input" type="password" name="token" required maxLength={4096}
        autoComplete="off" autoCapitalize="none" spellCheck={false} autoFocus disabled={busy}
        value={token} onChange={event => setToken(event.target.value)} /></Field>
      <button className="btn btn-primary login-submit" type="submit" disabled={busy}>
        {busy ? <><LoaderCircle size={16} className="spin" />{t('正在验证')}</>
          : <>{t(twoSteps ? '验证并继续' : '登录')}<ArrowRight size={16} /></>}
      </button>
    </form>
  </LoginLayout>
}
