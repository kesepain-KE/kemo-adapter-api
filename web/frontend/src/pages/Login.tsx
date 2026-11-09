import { useEffect, useState } from 'react'
import { api, setCsrf, useAction, useConsole } from '../console'
import { ErrorBar, Field } from '../components/Form'
import { getLanguage, languages, setLanguage, t } from '../i18n'
import { Select } from '../components/Select'

export default function Login() {
  const { refresh } = useConsole()
  const [methods, setMethods] = useState({ token_required: false, password_required: false })
  const [token, setToken] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const action = useAction()
  useEffect(() => { void action.run(async () => setMethods(await api('/auth/methods'))) }, [])
  return <div className="login-page"><form className="card login-card" onSubmit={e => {
    e.preventDefault()
    void action.run(async () => {
      if (methods.token_required) {
        const session = await api('/auth/token', { method: 'POST', body: JSON.stringify({ token }) })
        if (session.csrf_token) setCsrf(session.csrf_token)
      }
      if (methods.password_required) {
        const session = await api('/auth/password', { method: 'POST', body: JSON.stringify({ username, password }) })
        setCsrf(session.csrf_token)
      }
      await refresh()
    })
  }}>
    <h1>Kemo Gateway</h1><p className="muted">{t('登录管理控制台')}</p>
    <Select aria-label={t('语言')} value={getLanguage()} onChange={value => setLanguage(value as typeof languages[number]['code'])}
      options={languages.map(language => ({ value: language.code, label: language.name }))} />
    <ErrorBar error={action.error} />
    {methods.token_required && <Field label="Web Token"><input className="input" type="password" required autoComplete="off" value={token} onChange={e => setToken(e.target.value)} /></Field>}
    {methods.password_required && <>
      <Field label="用户名"><input className="input" autoComplete="username" required value={username} onChange={e => setUsername(e.target.value)} /></Field>
      <Field label="密码"><input className="input" type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} /></Field>
    </>}
    <button className="btn btn-primary" disabled={action.busy}>{t('登录')}</button>
  </form></div>
}
