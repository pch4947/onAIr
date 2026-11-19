// 마이페이지 · 내가 보낸 것 (#325) — 채팅·사연·신청곡과 처리 상태, 투표. 지난 방송 것도 함께 최근 순으로
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { STATUS_VARIANT_CLASS, statusLabel, statusVariant } from '@/routes/listen/requestStatus'
import { getSentItems, type SentItem } from '@/shared/account'

const PAGE_SIZE = 10
const KIND_LABEL: Record<string, string> = { story: '채팅', chat: '채팅', message: '사연', song_request: '신청곡' }

function when(item: SentItem) {
  const date = item.type === 'vote' ? new Date(item.at * 1000) : new Date(item.createdAt)
  return date.toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function Badge({ className, children }: { className: string; children: string }) {
  return <span className={`shrink-0 rounded-xl px-2 py-0.5 text-[10px] font-semibold text-white ${className}`}>{children}</span>
}

export function SentCollection() {
  const [page, setPage] = useState(1)
  const query = useQuery({
    queryKey: ['account-sent', page],
    queryFn: () => getSentItems((page - 1) * PAGE_SIZE, PAGE_SIZE),
  })
  const items = query.data?.items ?? []
  const totalPages = Math.max(1, Math.ceil((query.data?.total ?? 0) / PAGE_SIZE))

  if (query.isError) return <p role="alert" className="text-[13px] text-danger">불러오지 못했어요. 잠시 후 다시 시도해 주세요.</p>
  if (!query.isLoading && items.length === 0) {
    return <p className="text-[13px] text-text-muted">아직 방송에 보낸 채팅·사연·신청곡이 없어요</p>
  }

  return (
    <div className="flex flex-col gap-4">
      <ul className="flex flex-col gap-2.5">
        {items.map((item) => (
          <li key={`${item.type}-${item.stationId}-${item.id}`}
            className="flex flex-col gap-1 rounded-md border border-field-border bg-field-bg px-4 py-3.5">
            <div className="flex items-center justify-between gap-2">
              <span className="flex min-w-0 items-center gap-1.5 text-xs font-semibold text-text-muted">
                {item.type === 'vote' ? '투표' : KIND_LABEL[item.kind] ?? item.kind}
                {item.type === 'request' && item.anonymous && <span className="font-medium">· 익명</span>}
                <span className="font-medium">· {when(item)}</span>
              </span>
              {item.type === 'request'
                ? <Badge className={STATUS_VARIANT_CLASS[statusVariant(item.kind, item.status)]}>{statusLabel(item.kind, item.status)}</Badge>
                : <Badge className={item.status === 'closed' ? 'bg-status-played' : 'bg-status-queued'}>{item.status === 'closed' ? '마감' : '진행 중'}</Badge>}
            </div>
            {item.type === 'request' ? (
              <p className="wrap-break-word text-sm text-text">
                {item.kind === 'song_request' && item.trackTitle
                  ? `${item.trackTitle}${item.trackArtist ? ` — ${item.trackArtist}` : ''}`
                  : item.prompt}
              </p>
            ) : (
              <p className="wrap-break-word text-sm text-text">
                {item.question ?? '투표'} <span className="text-text-muted">→ {item.choice}</span>
              </p>
            )}
          </li>
        ))}
      </ul>

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-3">
          <button type="button" onClick={() => setPage((prev) => Math.max(1, prev - 1))} disabled={page <= 1}
            className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40">
            이전
          </button>
          <span className="text-xs text-text-muted">{page} / {totalPages}</span>
          <button type="button" onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))} disabled={page >= totalPages}
            className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40">
            다음
          </button>
        </div>
      )}
    </div>
  )
}
