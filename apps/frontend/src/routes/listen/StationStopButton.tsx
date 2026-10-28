import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createStationClient, StationApiError } from '@/routes/station-create/client'
import { getAccessToken } from '@/shared/authToken'

const client = createStationClient(import.meta.env.VITE_API_BASE_URL ?? '', getAccessToken)

export function StationStopButton({ stationId, userId }: { stationId?: string; userId?: string }) {
  const [confirming, setConfirming] = useState(false)
  const [targetCreatedAt, setTargetCreatedAt] = useState<number | null>(null)
  const cache = useQueryClient()
  const station = useQuery({
    queryKey: ['owned-station', stationId, userId],
    enabled: Boolean(stationId && userId),
    queryFn: async () => {
      try { return await client.getStation(stationId!) }
      catch (error) {
        if (error instanceof StationApiError && error.status === 404) return null
        throw error
      }
    },
    retry: false,
    refetchInterval: 3000,
  })
  const stop = useMutation({
    mutationFn: (createdAt: number) => client.stopStation(stationId!, createdAt),
    onSuccess: async () => {
      setConfirming(false)
      await Promise.all([
        cache.invalidateQueries({ queryKey: ['owned-station'] }),
        cache.invalidateQueries({ queryKey: ['broadcast-state'] }),
      ])
    },
  })
  if (!userId || !station.data || station.data.status === 'ended') return null
  const pending = ['stopping', 'cleaning'].includes(station.data.status)
  return <>
    <button type="button" disabled={pending} onClick={() => {
      stop.reset(); setTargetCreatedAt(station.data!.created_at); setConfirming(true)
    }}
      className="rounded bg-danger px-3 py-1 text-xs text-white disabled:opacity-60">
      {pending ? '방송 종료 중…' : '방송 종료'}
    </button>
    {confirming && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <section role="dialog" aria-modal="true" aria-labelledby="stop-title"
        className="max-w-sm rounded-lg bg-surface p-6 text-text shadow-xl">
        <h2 id="stop-title" className="font-semibold">방송을 종료할까요?</h2>
        <p className="my-4 text-sm">송출을 종료하고 대기 중인 신청곡과 요청을 정리합니다. 다시듣기와 방송 기록은 보존됩니다. 정리가 끝나면 새 방송을 만들 수 있어요.</p>
        {stop.isError && <p role="alert" className="mb-3 text-sm text-danger">종료 요청을 확인하지 못했어요. 상태를 확인하고 다시 시도해주세요.</p>}
        <div className="flex justify-end gap-3">
          <button type="button" disabled={stop.isPending} onClick={() => setConfirming(false)}>취소</button>
          <button type="button" disabled={stop.isPending} className="rounded bg-danger px-3 py-2 text-white"
            onClick={() => targetCreatedAt !== null && stop.mutate(targetCreatedAt)}>
            {stop.isPending ? '요청 중…' : '종료하고 정리'}
          </button>
        </div>
      </section>
    </div>}
  </>
}
