// 현재 코너와 채팅 반영 방식 안내 — 값의 뜻은 docs/CORNER_DESIGN.md 4장
import type { CornerInputMode, CurrentCorner, StationFormat } from '@/shared/types'

const CHAT_GUIDE: Partial<Record<CornerInputMode, string>> = {
  quote: '채팅이 방송에서 소개될 수 있어요',
  steer: '채팅이 소개되고, 이야기 주제가 될 수도 있어요',
  solicit: 'DJ의 질문에 채팅으로 답해 주세요',
  closed: '지금은 채팅이 방송에 반영되지 않아요',
}

interface CornerGuideProps {
  corner?: CurrentCorner | null
  stationFormat?: StationFormat | null
}

export function CornerGuide({ corner, stationFormat }: CornerGuideProps) {
  if (stationFormat === 'news') {
    return (
      <div className="rounded-md bg-field-bg px-3 py-2 text-[11px] text-text-muted">
        이 방송은 청취자 의견을 방송에 반영하지 않아요.
      </div>
    )
  }
  if (!corner) return null

  const chatMode = corner.contract.chat
  const hint = chatMode ? CHAT_GUIDE[chatMode] : undefined

  return (
    <div className="flex flex-col gap-1 rounded-md bg-primary-soft px-3 py-2 text-[11px]">
      <p className="font-semibold text-primary">지금 · {corner.title}</p>
      {hint && <p className="text-text-muted">{hint}</p>}
    </div>
  )
}
