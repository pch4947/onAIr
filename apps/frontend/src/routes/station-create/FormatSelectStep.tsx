// 00b_Main_방송 시작하기 — 형식 선택 단계 (이슈 #102, docs/CORNER_DESIGN.md 8절)
// 형식 3종만 고르고, 형식별 세부 선택지는 이어지는 PersonaFormStep(방송 시작하기) 안에서 보여준다.

import { useState } from 'react'
import type { StationFormat } from '@/shared/types'

const FORMAT_OPTIONS: { value: StationFormat; label: string; description: string }[] = [
  { value: 'music', label: '음악', description: '곡 소개와 신청곡 위주로 흘러가는 방송이에요' },
  { value: 'talk', label: '사연토크', description: '사연·문자·주제 토크 등 청취자 참여가 중심이에요' },
  { value: 'news', label: '뉴스', description: '분야별 소식을 전하는 방송이에요 (청취자 채팅은 반영하지 않아요)' },
]

interface FormatSelectStepProps {
  initialFormat?: StationFormat
  onSubmit: (format: StationFormat) => void
}

export function FormatSelectStep({ initialFormat = 'talk', onSubmit }: FormatSelectStepProps) {
  const [format, setFormat] = useState<StationFormat>(initialFormat)

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit(format)
      }}
      className="flex w-full flex-col gap-5"
    >
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold text-text">방송 형식을 골라주세요</h1>
        <p className="text-[13px] text-text-muted">형식에 따라 코너 구성과 청취자 참여 방식이 달라져요</p>
      </div>

      <div className="flex flex-col gap-2">
        {FORMAT_OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            aria-pressed={format === option.value}
            onClick={() => setFormat(option.value)}
            className={
              format === option.value
                ? 'flex flex-col gap-1 rounded-lg border-2 border-primary bg-primary/5 px-4 py-3 text-left'
                : 'flex flex-col gap-1 rounded-lg border border-field-border bg-field-bg px-4 py-3 text-left'
            }
          >
            <span className="text-sm font-semibold text-text">{option.label}</span>
            <span className="text-xs text-text-muted">{option.description}</span>
          </button>
        ))}
      </div>

      <button
        type="submit"
        className="mt-2 self-start rounded-md bg-primary px-5 py-3 text-sm font-semibold text-white"
      >
        다음
      </button>
    </form>
  )
}
