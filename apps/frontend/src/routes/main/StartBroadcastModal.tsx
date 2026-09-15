// 00b_Main_방송 시작하기

import { useState } from 'react'

const TONES = ['차분한 상담사', '발랄한 베프', '따뜻한 힐러', '유쾌한 예능인', '츤데레'] as const
const BOARD_READING_OPTIONS = ['읽음', '읽지 않음'] as const
const CHAT_OPTIONS = ['사용', '사용 안 함'] as const
const VISIBILITY_OPTIONS = ['공개방', '비공개방'] as const
const PARTICIPANT_LIMIT_OPTIONS = ['제한 없음', '인원 제한'] as const

interface ToggleChipsProps<T extends string> {
  options: readonly T[]
  value: T
  onChange: (value: T) => void
}

function ToggleChips<T extends string>({ options, value, onChange }: ToggleChipsProps<T>) {
  return (
    <div className="flex flex-wrap gap-2">
      {options.map((option) => (
        <button
          key={option}
          type="button"
          aria-pressed={value === option}
          onClick={() => onChange(option)}
          className={
            value === option
              ? 'rounded-xl bg-primary px-3.5 py-2 text-xs font-semibold text-white'
              : 'rounded-xl bg-chip-bg px-3.5 py-2 text-xs font-medium text-chip-text'
          }
        >
          {option}
        </button>
      ))}
    </div>
  )
}

const fieldLabelClass = 'text-[13px] font-medium text-label'
const textInputClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-[13px] text-text placeholder:text-field-placeholder'

interface StartBroadcastModalProps {
  onClose: () => void
}

export function StartBroadcastModal({ onClose }: StartBroadcastModalProps) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [tone, setTone] = useState<(typeof TONES)[number]>('차분한 상담사')
  const [boardReading, setBoardReading] = useState<(typeof BOARD_READING_OPTIONS)[number]>('읽음')
  const [chatUsage, setChatUsage] = useState<(typeof CHAT_OPTIONS)[number]>('사용')
  const [visibility, setVisibility] = useState<(typeof VISIBILITY_OPTIONS)[number]>('공개방')
  const [participantLimit, setParticipantLimit] =
    useState<(typeof PARTICIPANT_LIMIT_OPTIONS)[number]>('제한 없음')
  const [participantCount, setParticipantCount] = useState('')
  const [startTime, setStartTime] = useState('20:00')
  const [duration, setDuration] = useState('2시간')

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    onClose()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-6">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-[560px] flex-col gap-4.5 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg"
      >
        <header className="flex items-center justify-between">
          <p className="text-lg font-semibold text-text">🎙 방송 시작하기</p>
          <button type="button" onClick={onClose} className="text-base text-text-muted">
            ×
          </button>
        </header>

        <label className="flex flex-col gap-1.5">
          <span className={fieldLabelClass}>방송 제목</span>
          <input
            type="text"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="예: 감성 발라드 나이트"
            className={textInputClass}
          />
        </label>

        <label className="flex flex-col gap-1.5">
          <span className={fieldLabelClass}>방송 설명 (해시태그)</span>
          <input
            type="text"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="#감성발라드 #새벽감성 #잔잔한"
            className={textInputClass}
          />
        </label>

        <div className="flex flex-col gap-2">
          <span className={fieldLabelClass}>AI 성격</span>
          <ToggleChips options={TONES} value={tone} onChange={setTone} />
        </div>

        <div className="flex gap-3">
          <div className="flex flex-1 flex-col gap-2">
            <span className={fieldLabelClass}>게시판 사연 읽기</span>
            <ToggleChips
              options={BOARD_READING_OPTIONS}
              value={boardReading}
              onChange={setBoardReading}
            />
          </div>
          <div className="flex flex-1 flex-col gap-2">
            <span className={fieldLabelClass}>채팅 사용</span>
            <ToggleChips options={CHAT_OPTIONS} value={chatUsage} onChange={setChatUsage} />
          </div>
        </div>

        <div className="flex gap-3">
          <div className="flex flex-1 flex-col gap-2">
            <span className={fieldLabelClass}>공개 설정</span>
            <ToggleChips options={VISIBILITY_OPTIONS} value={visibility} onChange={setVisibility} />
          </div>
          <div className="flex flex-1 flex-col gap-2">
            <span className={fieldLabelClass}>인원 제한</span>
            <div className="flex items-start gap-2">
              <ToggleChips
                options={PARTICIPANT_LIMIT_OPTIONS}
                value={participantLimit}
                onChange={setParticipantLimit}
              />
              <input
                type="text"
                value={participantCount}
                onChange={(event) => setParticipantCount(event.target.value)}
                placeholder="예: 50명"
                className="h-9 w-[120px] rounded-lg border border-field-border bg-field-bg px-3.5 text-[13px] text-text placeholder:text-field-placeholder"
              />
            </div>
          </div>
        </div>

        <div className="flex flex-col gap-2">
          <span className={fieldLabelClass}>방송 시간</span>
          <div className="flex gap-3">
            <label className="flex flex-1 flex-col gap-1.5">
              <span className={fieldLabelClass}>시작 시간</span>
              <input
                type="text"
                value={startTime}
                onChange={(event) => setStartTime(event.target.value)}
                className={textInputClass}
              />
            </label>
            <label className="flex flex-1 flex-col gap-1.5">
              <span className={fieldLabelClass}>방송 길이</span>
              <input
                type="text"
                value={duration}
                onChange={(event) => setDuration(event.target.value)}
                className={textInputClass}
              />
            </label>
          </div>
        </div>

        <footer className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-4 py-2.5 text-[13px] font-medium text-text-muted"
          >
            취소
          </button>
          <button
            type="submit"
            className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white"
          >
            방송 시작
          </button>
        </footer>
      </form>
    </div>
  )
}
