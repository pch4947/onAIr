// 청취 화면 투표 카드 (#279) — 질문, 선택지 두 개, 남은 시간. 결과 숫자는 띄우지 않는다(DJ가 방송에서 발표)

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { isUnauthorizedError, LoginExpiredNotice } from '@/routes/listen/LoginExpiredNotice'
import { castVote, getCurrentVote, VoteError } from '@/shared/api'

const VOTE_POLL_INTERVAL_MS = 2000

export function VoteCard({ stationId, userId }: { stationId: string; userId?: string }) {
  const cache = useQueryClient()
  const queryKey = ['vote', stationId, userId]
  const voteQuery = useQuery({
    queryKey,
    queryFn: () => getCurrentVote(stationId),
    enabled: Boolean(stationId && userId),
    refetchInterval: VOTE_POLL_INTERVAL_MS,
  })
  const vote = voteQuery.data?.vote

  // 남은 시간은 받은 시점의 remaining_sec에서 1초씩 줄인다 — 서버와 기기 시계가 달라도 맞는다
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const remaining = vote ? Math.max(0, Math.ceil(vote.remaining_sec - (now - voteQuery.dataUpdatedAt) / 1000)) : 0

  const voteMutation = useMutation({
    mutationFn: (option: string) => castVote(stationId, vote!.vote_id, option),
    onSettled: () => cache.invalidateQueries({ queryKey }),
  })
  // 이미 투표했는데 다른 선택지를 누르면(409) 처음 고른 것을 보여 준다
  const conflictMine = voteMutation.error instanceof VoteError ? voteMutation.error.mine : null
  const mine = vote?.mine ?? voteMutation.data?.mine ?? conflictMine ?? null

  if (!vote || vote.status !== 'open' || remaining <= 0) return null

  const failed = voteMutation.isError && !conflictMine
  return (
    <section aria-label="투표" className="mx-3 mt-3 flex shrink-0 flex-col gap-2 rounded-md bg-primary-soft px-3 py-2.5 text-[11px]">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-primary">투표</span>
        <span className="tabular-nums text-text-muted">{remaining}초 남음</span>
      </div>
      <p className="text-[13px] font-semibold text-text">{vote.question}</p>
      <div className="grid grid-cols-2 gap-2">
        {vote.options.map((option) => {
          const chosen = mine === option
          return (
            <button
              key={option}
              type="button"
              aria-pressed={chosen}
              disabled={Boolean(mine) || voteMutation.isPending}
              onClick={() => voteMutation.mutate(option)}
              className={`rounded-md border px-3 py-2 text-xs font-semibold ${
                chosen ? 'border-primary bg-primary text-white' : 'border-border bg-surface text-text disabled:opacity-60'
              }`}
            >
              {option}
            </button>
          )
        })}
      </div>
      {mine && <p role="status" className="text-text-muted">투표에 반영됐어요. 결과는 DJ가 방송에서 알려 줘요.</p>}
      {failed && (isUnauthorizedError(voteMutation.error)
        ? <LoginExpiredNotice />
        : <p role="alert" className="text-danger">투표하지 못했어요. 다시 눌러 주세요.</p>)}
    </section>
  )
}
