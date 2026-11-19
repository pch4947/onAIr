// 설정 (#324) — 이 기기·브라우저에 저장되는 화면 설정. 고른 값은 "저장"을 눌러야 적용된다
import { useEffect, useState, type ReactNode } from 'react'
import {
  DEFAULT_SETTINGS, updateSettings, useSettings, type ChatFontSize, type DisplaySettings,
} from '@/shared/settings'

const SAVED_NOTICE_MS = 2500
const FONT_OPTIONS: { value: ChatFontSize; label: string }[] = [
  { value: 'small', label: '작게' },
  { value: 'normal', label: '보통' },
  { value: 'large', label: '크게' },
]

const sameSettings = (a: DisplaySettings, b: DisplaySettings) =>
  (Object.keys(a) as (keyof DisplaySettings)[]).every((key) => a[key] === b[key])

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-1">
      <h2 className="text-xs font-semibold text-text-muted">{title}</h2>
      <div className="flex flex-col divide-y divide-border rounded-md border border-border bg-surface">{children}</div>
    </section>
  )
}

// wide: 슬라이더처럼 폭이 필요한 조작은 좁은 화면에서 설명 아래로 내려 전체 폭을 쓴다
function Row({ label, description, wide = false, children }: {
  label: string; description?: string; wide?: boolean; children: ReactNode
}) {
  return (
    <div className={`flex justify-between gap-4 px-4 py-3.5 ${wide ? 'flex-col gap-3 sm:flex-row sm:items-center' : 'items-center'}`}>
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="text-sm font-medium text-text">{label}</span>
        {description && <span className="text-xs text-text-muted">{description}</span>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (next: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 rounded-full transition-colors ${checked ? 'bg-primary' : 'bg-field-border'}`}
    >
      <span
        className={`absolute top-0.5 size-5 rounded-full bg-white shadow transition-[left] ${checked ? 'left-5.5' : 'left-0.5'}`}
      />
    </button>
  )
}

export function SettingsPanel() {
  const saved = useSettings()
  // 저장하기 전까지 고른 값. 아무것도 바꾸지 않았으면 저장된 값(다른 탭·방송 화면에서 바꾼 음량 포함)을 그대로 보여 준다
  const [draft, setDraft] = useState<DisplaySettings | null>(null)
  const [savedNotice, setSavedNotice] = useState(false)
  const shown = draft ?? saved
  const dirty = draft !== null && !sameSettings(draft, saved)
  const change = (patch: Partial<DisplaySettings>) => {
    setDraft({ ...shown, ...patch })
    setSavedNotice(false)
  }
  const volumePercent = Math.round(shown.volume * 100)

  const handleSave = () => {
    if (!draft) return
    updateSettings(draft)
    setDraft(null)
    setSavedNotice(true)
  }

  useEffect(() => {
    if (!savedNotice) return
    const timer = setTimeout(() => setSavedNotice(false), SAVED_NOTICE_MS)
    return () => clearTimeout(timer)
  }, [savedNotice])

  // 저장하지 않고 창을 닫거나 새로고침하면 브라우저가 한 번 묻는다
  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  return (
    <div className="flex max-w-xl flex-col gap-6">
      <Section title="소리">
        <Row label="기본 음량" description="방송에 들어올 때 이 음량으로 시작해요. 방송 화면에서 바꾼 음량도 기억해요" wide>
          <div className="flex items-center gap-2">
            <input
              type="range"
              min={0}
              max={100}
              step={5}
              value={volumePercent}
              onChange={(event) => change({ volume: Number(event.target.value) / 100 })}
              aria-label="기본 음량"
              className="min-w-0 flex-1 accent-primary sm:w-28 sm:flex-none"
            />
            <span className="w-9 text-right text-xs tabular-nums text-text-muted">{volumePercent}%</span>
          </div>
        </Row>
        <Row label="자동 재생" description="방송에 들어가면 바로 재생해요. 브라우저가 막으면 재생 버튼을 눌러 주세요">
          <Toggle label="자동 재생" checked={shown.autoplay} onChange={(autoplay) => change({ autoplay })} />
        </Row>
      </Section>

      <Section title="채팅">
        <Row label="채팅 숨기기" description="방송에 들어갈 때 채팅창을 숨겨요. 방송 화면의 채팅 토글로도 켜고 끌 수 있어요">
          <Toggle label="채팅 숨기기" checked={shown.chatHidden} onChange={(chatHidden) => change({ chatHidden })} />
        </Row>
        <Row label="채팅 글자 크기">
          <div role="radiogroup" aria-label="채팅 글자 크기" className="flex rounded-md border border-border p-0.5">
            {FONT_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={shown.chatFontSize === option.value}
                onClick={() => change({ chatFontSize: option.value })}
                className={`rounded px-3 py-1 text-xs font-semibold ${
                  shown.chatFontSize === option.value ? 'bg-primary text-white' : 'text-text-muted'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </Row>
      </Section>

      <Section title="알림">
        <Row label="방송 알림" description="내 채팅·사연·신청곡이 방송에 나오면 화면에 알려 줘요">
          <Toggle label="방송 알림" checked={shown.airedToast} onChange={(airedToast) => change({ airedToast })} />
        </Row>
      </Section>

      <p className="text-xs text-text-muted">설정은 이 기기·브라우저에만 저장돼요.</p>

      <footer className="flex items-center justify-end gap-2">
        <span role="status" className="mr-auto text-xs font-semibold text-primary">
          {dirty ? '저장하지 않은 변경이 있어요' : savedNotice ? '저장했어요' : ''}
        </span>
        <button
          type="button"
          onClick={() => change(DEFAULT_SETTINGS)}
          disabled={sameSettings(shown, DEFAULT_SETTINGS)}
          className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-50"
        >
          기본값으로
        </button>
        <button
          type="button"
          onClick={handleSave}
          disabled={!dirty}
          className="rounded-md bg-primary px-5 py-2 text-xs font-semibold text-white disabled:opacity-50"
        >
          저장
        </button>
      </footer>
    </div>
  )
}
