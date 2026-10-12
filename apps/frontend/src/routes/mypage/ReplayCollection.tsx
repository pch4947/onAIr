import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { deleteReplay, unbookmarkReplay } from '@/shared/api'
import { ReplayListItem } from '@/routes/listen/ReplayListItem'
import type { ReplaySession } from '@/shared/types'

const PAGE_SIZE = 10

interface ReplayCollectionProps {
  queryKey: string
  fetcher: (params: { offset?: number; limit?: number }) => Promise<{ replays: ReplaySession[]; total: number }>
  emptyText: string
  // 'unbookmark': 저장한 방송 다시듣기 탭 — 삭제 버튼이 곧 "저장 해제"
  // 'destroy': 내 방송 다시듣기 탭 — 삭제 버튼이 녹음 자체를 영구 삭제
  deleteMode: 'unbookmark' | 'destroy'
}

export function ReplayCollection({ queryKey, fetcher, emptyText, deleteMode }: ReplayCollectionProps) {
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [confirmTarget, setConfirmTarget] = useState<ReplaySession | null>(null)
  const apiBase = import.meta.env.VITE_API_BASE_URL || window.location.origin
  const query = useQuery({
    queryKey: [queryKey, page],
    queryFn: () => fetcher({ offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE }),
  })
  const replays = query.data?.replays ?? []
  const totalPages = Math.max(1, Math.ceil((query.data?.total ?? 0) / PAGE_SIZE))

  // Reconcile the selected page when a refetch reports a smaller collection.
  if (query.data && page > totalPages) setPage(totalPages)

  const removeFromList = () => {
    // Both lists and all cached pages must reflect deletion/bookmark changes.
    if (replays.length === 1 && page > 1) setPage(page - 1)
    void queryClient.invalidateQueries({ queryKey: ['my-station-replays'] })
    void queryClient.invalidateQueries({ queryKey: ['bookmarked-replays'] })
  }

  const unbookmarkDeleteMutation = useMutation({
    mutationFn: (replay: ReplaySession) => unbookmarkReplay(replay.id),
    onSuccess: () => removeFromList(),
  })

  const destroyDeleteMutation = useMutation({
    mutationFn: (replay: ReplaySession) => deleteReplay(replay.id),
    onSuccess: () => {
      removeFromList()
      setConfirmTarget(null)
    },
    onError: () => setConfirmTarget(null),
  })
  const deleteMutation = deleteMode === 'unbookmark' ? unbookmarkDeleteMutation : destroyDeleteMutation

  const handleDeleteClick = (replay: ReplaySession) => {
    if (deleteMode === 'destroy') {
      setConfirmTarget(replay)
      return
    }
    deleteMutation.mutate(replay)
  }

  if (query.isError) {
    return <p className="text-[13px] text-danger">목록을 불러오지 못했어요</p>
  }

  if (!query.isLoading && replays.length === 0 && page === 1) {
    return <p className="text-[13px] text-text-muted">{emptyText}</p>
  }

  return (
    <div className="flex flex-col gap-4">
      {deleteMutation.isError && (
        <p className="text-[13px] text-danger">
          {(deleteMutation.error as Error).message || '삭제에 실패했어요, 다시 시도해주세요'}
        </p>
      )}
      <ul className="flex flex-col gap-2.5">
        {replays.map((replay) => (
          <ReplayListItem key={replay.id} replay={replay} apiBase={apiBase} onDelete={handleDeleteClick} />
        ))}
      </ul>

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-3">
          <button
            type="button"
            onClick={() => setPage((prev) => Math.max(1, prev - 1))}
            disabled={page <= 1}
            className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
          >
            이전
          </button>
          <span className="text-xs text-text-muted">
            {page} / {totalPages}
          </span>
          <button
            type="button"
            onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
            disabled={page >= totalPages}
            className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
          >
            다음
          </button>
        </div>
      )}

      {confirmTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
          <div className="flex w-full max-w-100 flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg">
            <p className="text-lg font-semibold text-text">이 방송 다시듣기를 삭제할까요?</p>
            <p className="text-[13px] leading-relaxed text-text-muted">
              삭제하면 녹음이 영구히 사라지고, 이 방송을 저장해 둔 다른 사람들의{' '}
              <span className="font-semibold text-primary">저장한 방송 다시듣기</span> 목록에서도
              함께 삭제돼요. 되돌릴 수 없어요.
            </p>
            <footer className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmTarget(null)}
                disabled={destroyDeleteMutation.isPending}
                className="rounded-md px-4 py-2.5 text-[13px] font-medium text-text-muted"
              >
                취소
              </button>
              <button
                type="button"
                onClick={() => destroyDeleteMutation.mutate(confirmTarget)}
                disabled={destroyDeleteMutation.isPending}
                className="rounded-md bg-danger px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
              >
                {destroyDeleteMutation.isPending ? '삭제 중...' : '삭제'}
              </button>
            </footer>
          </div>
        </div>
      )}
    </div>
  )
}
