import { useEffect } from 'react'
import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { InfoIcon } from '@/routes/listen/icons'
import { curatedVoiceLabel } from '@/routes/station-create/voiceOptions'
import { ApiError, getStationDj } from '@/shared/api'
import { styleTags } from '@/shared/personaLabels'
import type { StationFormat } from '@/shared/types'

// 방송 만들기의 형식 이름과 같게 둔다 (FormatSelectStep)
const FORMAT_LABEL: Record<StationFormat, string> = { talk: '사연토크', music: '음악', news: '뉴스' }

interface DjInfoModalProps {
  stationId: string
  // 지금 방송 중인 DJ 이름 (/stream/state). 페르소나 전환(#270)으로 바뀌면 새로 불러온다
  djName?: string | null
  onClose: () => void
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <dt className="text-[11px] font-semibold text-text-muted">{label}</dt>
      <dd className="flex flex-col gap-1.5 text-[13px] break-keep wrap-break-word text-text">{children}</dd>
    </div>
  )
}

function Tags({ tags }: { tags: string[] }) {
  return (
    <ul className="flex flex-wrap gap-1.5">
      {tags.map((tag) => (
        <li key={tag} className="rounded-xl bg-primary-soft px-2 py-0.5 text-[11px] font-semibold text-primary">{tag}</li>
      ))}
    </ul>
  )
}

// 청취 화면의 DJ 정보 창 (#353) — 방송 만들기에서 정한 DJ의 캐릭터를 청취자에게 보여 준다
export function DjInfoModal({ stationId, djName, onClose }: DjInfoModalProps) {
  const djQuery = useQuery({
    queryKey: ['station-dj', stationId, djName ?? null],
    queryFn: () => getStationDj(stationId),
    // DJ가 없는 방송(404)은 다시 물어도 없다
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2,
  })

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose])

  const info = djQuery.data
  const dj = info?.dj
  const tags = styleTags(dj?.style)
  const voice = curatedVoiceLabel(dj?.voice)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6" onClick={onClose}>
      <section
        role="dialog" aria-modal="true" aria-labelledby="dj-info-title"
        onClick={(event) => event.stopPropagation()}
        className="flex max-h-[90dvh] w-full max-w-100 flex-col gap-5 overflow-y-auto rounded-xl bg-surface px-7 pb-7 pt-7 shadow-lg"
      >
        <header className="flex items-center justify-between">
          <p id="dj-info-title" className="flex items-center gap-1.5 text-lg font-semibold text-text">
            <InfoIcon className="size-4.5" />
            DJ 정보
          </p>
          <button type="button" onClick={onClose} aria-label="닫기" className="text-base text-text-muted">
            ×
          </button>
        </header>

        {djQuery.isPending && <p className="text-[13px] text-text-muted">DJ 정보를 불러오는 중이에요</p>}
        {djQuery.isError && (
          <div role="alert" className="flex items-center justify-between gap-3 text-[13px] text-danger">
            <span>
              {djQuery.error instanceof ApiError && djQuery.error.status === 404
                ? '이 방송의 DJ 정보가 없어요'
                : 'DJ 정보를 불러오지 못했어요'}
            </span>
            <button type="button" onClick={() => void djQuery.refetch()} className="shrink-0 text-xs font-semibold text-primary">
              다시 시도
            </button>
          </div>
        )}

        {info && dj && (
          <>
            <div className="flex flex-col gap-1">
              <p className="text-xl font-bold text-text">{dj.name}</p>
              {dj.concept && <p className="text-[13px] break-keep wrap-break-word text-text-muted">{dj.concept}</p>}
            </div>

            <dl className="flex flex-col gap-4">
              {(dj.tone || tags.length > 0) && (
                <Row label="말투">
                  {dj.tone && <span>{dj.tone}</span>}
                  {tags.length > 0 && <Tags tags={tags} />}
                </Row>
              )}
              {voice && <Row label="목소리">{voice}</Row>}
              {dj.musicTaste.length > 0 && (
                <Row label="선곡 취향">
                  <Tags tags={dj.musicTaste} />
                </Row>
              )}
              {dj.signaturePhrases.length > 0 && (
                <Row label="입버릇">
                  <ul className="flex flex-col gap-1">
                    {dj.signaturePhrases.map((phrase) => <li key={phrase}>“{phrase}”</li>)}
                  </ul>
                </Row>
              )}
              <Row label="방송">
                {FORMAT_LABEL[info.format] ?? info.format}{info.topic ? ` · ${info.topic}` : ''}
              </Row>
            </dl>
          </>
        )}
      </section>
    </div>
  )
}
