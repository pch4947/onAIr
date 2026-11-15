// 테스트 화면 — "테스트 방송 시작하기"로 테스트 방송을 열고, 방송 화면에 코너 변경·엔진 로그를 붙여 보여준다
// 테스트 방송(#306)은 실제 방송과 따로다: 홈 목록·내 방송·1인 1방송 제한·다시듣기에 섞이지 않는다

import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Sidebar } from '@/app/Sidebar'
import { createStation, getMyStation, getVoices } from '@/routes/station-create/api'
import { StationApiError } from '@/routes/station-create/client'
import { ListenPage } from '@/routes/listen/ListenPage'
import { TestPanel } from '@/routes/test/TestPanel'
import { PERSONA_PRESETS, presetVoice, TEST_STATION } from '@/routes/test/presets'
import { TEST_STATION_KEY } from '@/routes/test/testStation'
import { useRealtimePollInterval } from '@/realtime/store'
import { getBroadcastState } from '@/shared/api'
import { randomId } from '@/shared/randomId'
import { useCurrentUser } from '@/shared/useCurrentUser'

const BROADCAST_STATE_POLL_INTERVAL_MS = 3000
const BUSY_STATUSES = new Set(['preparing', 'started', 'draining', 'stopping', 'cleaning'])

export function TestPage() {
  const cache = useQueryClient()
  const { data: currentUser } = useCurrentUser()
  const isAdmin = Boolean(currentUser?.isAdmin)
  const myStationQuery = useQuery({
    queryKey: ['my-test-station'],
    queryFn: () => getMyStation(true),
    enabled: isAdmin,
    refetchInterval: BROADCAST_STATE_POLL_INTERVAL_MS,
  })
  // 끝난 방송은 내 방송에서 빠지므로, 마지막으로 연 방송 ID를 기억해 '끝났어요'를 보여준다
  const [startedId, setStartedId] = useState<string | null>(null)
  const stationId = myStationQuery.data?.station_id ?? startedId ?? undefined
  // 방송이 열리면 그 안의 청취 화면이 웹소켓을 붙인다
  const broadcastPollMs = useRealtimePollInterval(BROADCAST_STATE_POLL_INTERVAL_MS)
  const broadcastStateQuery = useQuery({
    queryKey: ['broadcast-state', stationId],
    queryFn: () => getBroadcastState(stationId!),
    enabled: Boolean(stationId),
    refetchInterval: broadcastPollMs,
  })
  const voicesQuery = useQuery({ queryKey: ['voices'], queryFn: getVoices, enabled: isAdmin })
  const voice = presetVoice(voicesQuery.data, 0)
  const state = broadcastStateQuery.data
  const busy = BUSY_STATUSES.has(state?.stationStatus ?? '')

  const startMutation = useMutation({
    mutationFn: (voiceId: string) => createStation({
      ...TEST_STATION, intensity: 'mid', persona: { ...PERSONA_PRESETS[0].persona, voice: voiceId },
    }, randomId()),
    onSuccess: (station) => {
      // 새 방송이라 지난 방송 정보(설정)와, 같은 채널 지난 방송의 채팅 캐시를 버린다
      cache.removeQueries({ queryKey: [TEST_STATION_KEY] })
      cache.removeQueries({ queryKey: ['requests', station.station_id] })
      cache.removeQueries({ queryKey: ['test-requests', station.station_id] })
      setStartedId(station.station_id)
      return cache.invalidateQueries({ queryKey: ['my-test-station'] })
    },
    // 그새 다른 창에서 테스트 방송이 열렸으면(409) 그 방송을 쓴다
    onError: (error) => {
      if (error instanceof StationApiError && error.status === 409 && error.stationId) setStartedId(error.stationId)
    },
  })
  const { reset } = startMutation

  // 방송이 올라오면 시작 요청 상태를 비워, 콘솔에서 종료한 뒤 '끝났어요'로 보이게 한다
  useEffect(() => {
    if (busy) reset()
  }, [busy, reset])

  // 테스트 화면은 어드민만 쓴다. 메뉴는 숨겼지만 주소로 직접 들어와도 돌려보낸다
  if (currentUser && !currentUser.isAdmin) {
    return <Navigate to="/main" replace />
  }

  const failed = (startMutation.isError && !startedId) || voicesQuery.isError || myStationQuery.isError
  const ended = Boolean(state) && !busy && startMutation.isIdle
  // 아직 연 테스트 방송이 없다 — 들어오기만 해서는 열지 않고 버튼으로 연다
  const idle = myStationQuery.isSuccess && !stationId && startMutation.isIdle
  const loading = myStationQuery.isPending && isAdmin
  if (state && busy) {
    return <ListenPage navActive="test" stationId={state.stationId} testPanel={<TestPanel state={state} />} />
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar active="test" />

      <main className="flex flex-1 flex-col gap-6 bg-bg p-4 pb-24 md:p-8 md:pb-8">
        <header className="flex flex-col gap-1">
          <h1 className="text-center text-lg font-semibold text-text md:text-left md:text-[22px]">테스트</h1>
          <p className="text-center text-[13px] text-text-muted md:text-left">
            테스트 방송을 열면 방송 화면에서 코너를 바꾸고 엔진이 입력을 어떻게 다루는지 볼 수 있어요.
          </p>
        </header>

        <section className="flex flex-col items-center gap-3 rounded-lg border border-border bg-surface p-8 text-center">
          <p className={`text-[13px] ${failed ? 'text-danger' : 'text-text-muted'}`}>
            {failed
              ? startMutation.error instanceof StationApiError ? startMutation.error.message : '테스트 방송을 열지 못했어요.'
              : loading ? '불러오는 중…'
                : ended ? '테스트 방송이 끝났어요.'
                  : idle ? '테스트 방송은 홈 방송 목록에 나오지 않고, 다시듣기도 남기지 않아요.'
                    : '테스트 방송을 여는 중…'}
          </p>
          {voice && (failed || ended || idle) && (
            <button
              type="button"
              onClick={() => startMutation.mutate(voice)}
              className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white"
            >
              {failed ? '다시 시도' : '테스트 방송 시작하기'}
            </button>
          )}
        </section>
      </main>
    </div>
  )
}
