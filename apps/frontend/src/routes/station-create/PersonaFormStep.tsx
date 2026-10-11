import { useEffect, useState } from 'react'
import { GENRE_OPTIONS, MAX_MUSIC_TASTE, getVoices } from '@/routes/station-create/mock'
import type { Energy, Formality, Humor, PersonaForm, StationInfo, Voice } from '@/shared/types'

const FORMALITY_OPTIONS: { value: Formality; label: string }[] = [
  { value: 'polite', label: '존댓말' },
  { value: 'casual', label: '반말' },
]
const ENERGY_OPTIONS: { value: Energy; label: string }[] = [
  { value: 'low', label: '차분하게' },
  { value: 'mid', label: '보통' },
  { value: 'high', label: '활기차게' },
]
const HUMOR_OPTIONS: { value: Humor; label: string }[] = [
  { value: 'rare', label: '드물게' },
  { value: 'some', label: '가끔' },
  { value: 'often', label: '자주' },
]
const BROADCAST_MINUTES_OPTIONS = [30, 45, 60]

const fieldLabelClass = 'text-[13px] font-medium text-label'
const textInputClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-[13px] text-text placeholder:text-field-placeholder'
const selectInputClass =
  'h-10 w-full appearance-none rounded-lg border border-field-border bg-field-bg pl-3.5 pr-9 text-[13px] text-text'
const errorTextClass = 'block text-xs text-danger'

function SelectArrow() {
  return (
    <span className="pointer-events-none absolute right-3.5 top-1/2 -translate-y-1/2 text-base text-text-muted">
      ▾
    </span>
  )
}

function Chips<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[]
  value: T
  onChange: (value: T) => void
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          className={
            value === option.value
              ? 'rounded-xl bg-primary px-3.5 py-2 text-xs font-semibold text-white'
              : 'rounded-xl bg-chip-bg px-3.5 py-2 text-xs font-medium text-chip-text'
          }
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

interface PersonaFormStepProps {
  onSubmit: (form: PersonaForm, station: StationInfo) => void
}

export function PersonaFormStep({ onSubmit }: PersonaFormStepProps) {
  const [voices, setVoices] = useState<Voice[]>([])
  const [formality, setFormality] = useState<Formality>('polite')
  const [energy, setEnergy] = useState<Energy>('low')
  const [humor, setHumor] = useState<Humor>('rare')
  const [voice, setVoice] = useState('')
  const [musicTaste, setMusicTaste] = useState<string[]>([])
  const [djName, setDjName] = useState('')
  const [hostNote, setHostNote] = useState('')
  const [firstSongUrl, setFirstSongUrl] = useState('')
  const [broadcastMinutes, setBroadcastMinutes] = useState(30)
  const [topic, setTopic] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    let cancelled = false
    getVoices().then((result) => {
      if (cancelled) return
      setVoices(result)
      setVoice((current) => current || result[0]?.id || '')
    })
    return () => {
      cancelled = true
    }
  }, [])

  const toggleGenre = (genre: string) => {
    setMusicTaste((prev) => {
      if (prev.includes(genre)) return prev.filter((item) => item !== genre)
      if (prev.length >= MAX_MUSIC_TASTE) return prev
      return [...prev, genre]
    })
  }

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    const nextErrors: Record<string, string> = {}
    if (!voice) nextErrors.voice = '목소리를 선택해주세요'
    if (djName.trim().length > 20) nextErrors.djName = 'DJ 이름은 20자 이하로 입력해주세요'
    if (hostNote.length > 100) nextErrors.hostNote = '100자 이하로 입력해주세요'
    if (topic.length > 60) nextErrors.topic = '60자 이하로 입력해주세요'
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) return

    onSubmit(
      {
        style: { formality, energy, humor },
        musicTaste,
        voice,
        djName: djName.trim() || undefined,
        hostNote: hostNote.trim() || undefined,
      },
      {
        firstSongUrl: firstSongUrl.trim() || undefined,
        broadcastMinutes,
        topic: topic.trim() || undefined,
      },
    )
  }

  return (
    <form onSubmit={handleSubmit} className="flex w-full flex-col gap-5">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold text-text">방송 시작하기</h1>
        <p className="text-[13px] text-text-muted">
          DJ 스타일을 고르면 AI가 어울리는 캐릭터 초안을 만들어드려요
        </p>
      </div>

      <div className="flex flex-wrap gap-x-6 gap-y-3">
        <div className="flex flex-col gap-2">
          <span className={fieldLabelClass}>말투</span>
          <Chips options={FORMALITY_OPTIONS} value={formality} onChange={setFormality} />
        </div>

        <div className="flex flex-col gap-2">
          <span className={fieldLabelClass}>에너지</span>
          <Chips options={ENERGY_OPTIONS} value={energy} onChange={setEnergy} />
        </div>

        <div className="flex flex-col gap-2">
          <span className={fieldLabelClass}>유머</span>
          <Chips options={HUMOR_OPTIONS} value={humor} onChange={setHumor} />
        </div>
      </div>

      <label className="flex flex-col gap-1.5">
        <span className={fieldLabelClass}>목소리</span>
        <div className="relative">
          <select value={voice} onChange={(event) => setVoice(event.target.value)} className={selectInputClass}>
            {voices.length === 0 && <option value="">불러오는 중...</option>}
            {voices.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · {item.description}
              </option>
            ))}
          </select>
          <SelectArrow />
        </div>
        <span className={errorTextClass}>{errors.voice || ' '}</span>
      </label>

      <div className="flex flex-col gap-2">
        <span className={fieldLabelClass}>선곡 취향 (최대 {MAX_MUSIC_TASTE}개)</span>
        <div className="flex flex-wrap gap-2">
          {GENRE_OPTIONS.map((genre) => {
            const active = musicTaste.includes(genre)
            const disabled = !active && musicTaste.length >= MAX_MUSIC_TASTE
            return (
              <button
                key={genre}
                type="button"
                aria-pressed={active}
                disabled={disabled}
                onClick={() => toggleGenre(genre)}
                className={
                  active
                    ? 'rounded-xl bg-primary px-3.5 py-2 text-xs font-semibold text-white'
                    : 'rounded-xl bg-chip-bg px-3.5 py-2 text-xs font-medium text-chip-text disabled:opacity-40'
                }
              >
                {genre}
              </button>
            )
          })}
        </div>
      </div>

      <label className="flex flex-col gap-1.5">
        <span className={fieldLabelClass}>DJ 이름 (선택, 비우면 AI가 제안해요)</span>
        <input
          type="text"
          value={djName}
          onChange={(event) => setDjName(event.target.value)}
          placeholder="예: 새벽"
          maxLength={20}
          className={textInputClass}
        />
        <span className={errorTextClass}>{errors.djName || ' '}</span>
      </label>

      <label className="flex flex-col gap-1.5">
        <span className={fieldLabelClass}>원하는 DJ (선택)</span>
        <textarea
          value={hostNote}
          onChange={(event) => setHostNote(event.target.value)}
          placeholder="예: 공부하는 사람 옆에 조용히 있어 주는 DJ"
          maxLength={100}
          rows={2}
          className="h-16 w-full resize-none rounded-lg border border-field-border bg-field-bg px-3.5 py-2.5 text-[13px] text-text placeholder:text-field-placeholder"
        />
        <span className={errors.hostNote ? errorTextClass : 'block text-xs text-text-muted'}>
          {errors.hostNote || `${hostNote.length}/100`}
        </span>
      </label>

      <div className="h-px bg-border" />

      <label className="flex flex-col gap-1.5">
        <span className={fieldLabelClass}>첫 노래 (선택, YouTube 링크)</span>
        <input
          type="text"
          value={firstSongUrl}
          onChange={(event) => setFirstSongUrl(event.target.value)}
          placeholder="https://youtube.com/watch?v=..."
          className={textInputClass}
        />
      </label>

      <label className="flex flex-col gap-1.5">
        <span className={fieldLabelClass}>방송 시간</span>
        <div className="relative">
          <select
            value={broadcastMinutes}
            onChange={(event) => setBroadcastMinutes(Number(event.target.value))}
            className={selectInputClass}
          >
            {BROADCAST_MINUTES_OPTIONS.map((minutes) => (
              <option key={minutes} value={minutes}>
                {minutes}분
              </option>
            ))}
          </select>
          <SelectArrow />
        </div>
      </label>

      <label className="flex flex-col gap-1.5">
        <span className={fieldLabelClass}>오늘의 주제 (선택, 비우면 AI가 정해요)</span>
        <input
          type="text"
          value={topic}
          onChange={(event) => setTopic(event.target.value)}
          placeholder="예: 시험 기간을 버티는 나만의 방법"
          maxLength={60}
          className={textInputClass}
        />
        <span className={errorTextClass}>{errors.topic || ' '}</span>
      </label>

      <button
        type="submit"
        className="mt-2 self-start rounded-md bg-primary px-5 py-3 text-sm font-semibold text-white"
      >
        방송 시작하기
      </button>
    </form>
  )
}
