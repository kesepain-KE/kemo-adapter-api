import type { ReactNode } from 'react'
import { Check, LockKeyhole } from 'lucide-react'
import { getLanguage, languages, setLanguage, t } from '../i18n'
import { Select } from './Select'

const logo = new URL('../../../../llm-adapter-kemo.ico', import.meta.url).href

export function LoginLayout({ title, description, step, twoSteps = false, children }: {
  title: string; description: string; step?: 'token' | 'password'; twoSteps?: boolean; children: ReactNode
}) {
  return <main className="login-page"><section className="card login-card" aria-labelledby="login-title">
    <header className="login-brand">
      <img src={logo} alt="" width="42" height="42" />
      <span>Kemo Gateway</span>
    </header>
    {twoSteps && <ol className="login-steps" aria-label={t('登录步骤')}>
      <li className={step === 'token' ? 'current' : 'complete'} aria-current={step === 'token' ? 'step' : undefined}>
        <span className="login-step-number">{step === 'password' ? <Check size={12} /> : '1'}</span>{t('Token 认证')}
      </li>
      <li className={step === 'password' ? 'current' : ''} aria-current={step === 'password' ? 'step' : undefined}>
        <span className="login-step-number">2</span>{t('用户认证')}
      </li>
    </ol>}
    <div className="login-heading">
      <h1 id="login-title">{t(title)}</h1>
      <p>{t(description)}</p>
    </div>
    {children}
    <footer className="login-footer">
      <span><LockKeyhole size={13} />{t('管理控制台')}</span>
      <Select className="login-language" aria-label={t('显示语言')} value={getLanguage()}
        onChange={value => setLanguage(value as typeof languages[number]['code'])}
        options={languages.map(language => ({ value: language.code, label: language.name }))} />
    </footer>
  </section></main>
}
