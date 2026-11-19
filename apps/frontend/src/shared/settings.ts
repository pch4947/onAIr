// 화면 설정 (#324) — 계정이 아니라 이 기기·브라우저에 저장한다.
// 기본값은 설정이 생기기 전의 동작과 같다. 저장소를 못 쓰면(사생활 보호 모드 등) 기본값으로 동작한다
import { useSyncExternalStore } from 'react'

export type ChatFontSize = 'small' | 'normal' | 'large'

export interface DisplaySettings {
  volume: number
  autoplay: boolean
  chatHidden: boolean
  chatFontSize: ChatFontSize
  airedToast: boolean
}

export const DEFAULT_SETTINGS: DisplaySettings = {
  volume: 1,
  autoplay: true,
  chatHidden: false,
  chatFontSize: 'normal',
  airedToast: true,
}

const STORAGE_KEY = 'onair:display-settings'
const FONT_SIZES: ChatFontSize[] = ['small', 'normal', 'large']

// 예전 값이나 손으로 고친 값이 들어 있어도 항목마다 따로 검사해, 잘못된 항목만 기본값으로 둔다
function parse(raw: string | null): DisplaySettings {
  let saved: Partial<Record<keyof DisplaySettings, unknown>> = {}
  try {
    const value: unknown = raw ? JSON.parse(raw) : {}
    if (value && typeof value === 'object') saved = value
  } catch { /* 깨진 값은 버린다 */ }
  const bool = (key: 'autoplay' | 'chatHidden' | 'airedToast') =>
    typeof saved[key] === 'boolean' ? saved[key] : DEFAULT_SETTINGS[key]
  const volume = typeof saved.volume === 'number' && saved.volume >= 0 && saved.volume <= 1
    ? saved.volume : DEFAULT_SETTINGS.volume
  const chatFontSize = FONT_SIZES.includes(saved.chatFontSize as ChatFontSize)
    ? saved.chatFontSize as ChatFontSize : DEFAULT_SETTINGS.chatFontSize
  return { volume, autoplay: bool('autoplay'), chatHidden: bool('chatHidden'), chatFontSize, airedToast: bool('airedToast') }
}

function load(): DisplaySettings {
  try {
    return parse(window.localStorage.getItem(STORAGE_KEY))
  } catch {
    return DEFAULT_SETTINGS
  }
}

let current = load()
const listeners = new Set<() => void>()

function notify() {
  listeners.forEach((listener) => listener())
}

export function updateSettings(patch: Partial<DisplaySettings>) {
  current = { ...current, ...patch }
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(current))
  } catch { /* 이번 화면에서만 적용된다 */ }
  notify()
}

// 다른 탭에서 바꾼 설정도 따라간다
function onStorage(event: StorageEvent) {
  if (event.key !== STORAGE_KEY) return
  current = parse(event.newValue)
  notify()
}

function subscribe(listener: () => void) {
  if (listeners.size === 0) window.addEventListener('storage', onStorage)
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) window.removeEventListener('storage', onStorage)
  }
}

export function useSettings(): DisplaySettings {
  return useSyncExternalStore(subscribe, () => current)
}
