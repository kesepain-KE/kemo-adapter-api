import { createInstance } from 'i18next'
import { useSyncExternalStore } from 'react'
import { messages } from './messages'

export const languages = [
  { code: 'zh-CN', name: '简体中文' },
  { code: 'en', name: 'English' },
  { code: 'es', name: 'Español' },
  { code: 'fr', name: 'Français' },
  { code: 'de', name: 'Deutsch' },
  { code: 'pt-BR', name: 'Português (Brasil)' },
  { code: 'ru', name: 'Русский' },
  { code: 'ja', name: '日本語' },
  { code: 'ko', name: '한국어' },
] as const

export type Language = typeof languages[number]['code']
const storageKey = 'kemo.gateway.language'

export function normalizeLanguage(value: string | null | undefined): Language {
  const exact = languages.find(language => language.code.toLowerCase() === value?.toLowerCase())
  if (exact) return exact.code
  return languages.find(language => language.code.split('-')[0] === value?.split('-')[0])?.code ?? 'zh-CN'
}

function initialLanguage(): Language {
  try {
    const saved = localStorage.getItem(storageKey)
    if (saved) return normalizeLanguage(saved)
  } catch { /* Browser policy can disable preference storage. */ }
  return normalizeLanguage(navigator.language)
}

const i18n = createInstance()
void i18n.init({
  resources: Object.fromEntries(languages.map(({ code }, index) => [code, {
    translation: Object.fromEntries(Object.entries(messages).map(([source, translations]) => [
      source, index === 0 ? source : translations[index - 1],
    ])),
  }])),
  lng: initialLanguage(),
  fallbackLng: 'zh-CN',
  supportedLngs: languages.map(({ code }) => code),
  load: 'currentOnly',
  initAsync: false,
  keySeparator: false,
  nsSeparator: false,
  returnNull: false,
  returnEmptyString: false,
  interpolation: { prefix: '{', suffix: '}', escapeValue: false },
})

export function getLanguage(): Language {
  return i18n.language as Language
}

function applyDocumentLanguage() {
  document.documentElement.lang = getLanguage()
  document.documentElement.dir = 'ltr'
}

applyDocumentLanguage()

export function setLanguage(language: Language) {
  void i18n.changeLanguage(language)
  applyDocumentLanguage()
  try { localStorage.setItem(storageKey, language) } catch { /* Preference is session-only. */ }
}

const subscribe = (listener: () => void) => {
  i18n.on('languageChanged', listener)
  return () => { i18n.off('languageChanged', listener) }
}

export function useLanguage() {
  return useSyncExternalStore(subscribe, getLanguage)
}

/** Explicit first-party UI lookup. Never translate user or Provider content. */
export function t(source: string, values: Record<string, string | number> = {}): string {
  return i18n.t(source, { ...values, defaultValue: source })
}
