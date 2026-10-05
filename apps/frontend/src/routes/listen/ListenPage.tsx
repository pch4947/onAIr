// 01_Listen_스트리밍 방송 화면

import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Sidebar } from '@/app/Sidebar'
import { useLivePlayback } from '@/player/useLivePlayback'
import { useRealtimeConnection } from '@/realtime/useRealtimeConnection'
import { useRealtimeStore } from '@/realtime/store'
import { getBroadcastState, submitRequest } from '@/shared/api'
import { ExperimentSurveyModal } from '@/routes/listen/ExperimentSurveyModal'
import { SongRequestModal } from '@/routes/listen/SongRequestModal'
import { CHAT_MESSAGES, MY_REQUEST_STATUSES, STATION_INFO } from '@/routes/listen/mock'
import type { ChatMessage, RequestStatusVariant } from '@/routes/listen/mock'
import type { ConnectionStatus } from '@/realtime/client'
import type { StreamStatus } from '@/shared/types'

const STATUS_VARIANT_CLASS: Record<RequestStatusVariant, string> = {
  generating: 'bg-status-generating',
  queued: 'bg-status-queued',
  played: 'bg-status-played',
  rejected: 'bg-status-rejected',
  muted: 'bg-status-muted',
}

const CONNECTION_STATUS_LABEL: Record<ConnectionStatus, string> = {
  connecting: '연결 중',
  connected: '연결됨',
  disconnected: '연결 끊김',
}

const STREAM_STATUS_LABEL: Record<StreamStatus, string> = {
  bootstrapping: '부팅 중',
  ready: '준비됨',
  planning: '편성 중',
}

const BROADCAST_STATE_POLL_INTERVAL_MS = 3000

export function ListenPage() {
  const [isSurveyOpen, setIsSurveyOpen] = useState(false)
  const [isSongRequestOpen, setIsSongRequestOpen] = useState(false)
  const [chatInput, setChatInput] = useState('')
  const [sentMessages, setSentMessages] = useState<ChatMessage[]>([])
  useRealtimeConnection()
  const connectionStatus = useRealtimeStore((state) => state.connectionStatus)
  const broadcastStateQuery = useQuery({
    queryKey: ['broadcast-state'],
    queryFn: getBroadcastState,
    refetchInterval: BROADCAST_STATE_POLL_INTERVAL_MS,
  })

  const stream = broadcastStateQuery.data?.stream
  const apiBase = import.meta.env.VITE_API_BASE_URL || window.location.origin
  const hlsPath = import.meta.env.VITE_HLS_URL || stream?.hlsUrl
  const hlsUrl = stream?.status === 'ready' && hlsPath ? new URL(hlsPath, apiBase).href : null
  const { audioRef, isPlaying, error: playbackError, togglePlay } = useLivePlayback(hlsUrl)
  const submitRequestMutation = useMutation({
    mutationFn: submitRequest,
    onSuccess: (_, prompt) => {
      setSentMessages((prev) => [...prev, { id: crypto.randomUUID(), author: '나', text: prompt }])
    },
  })

  const handleSendChat = (event: React.FormEvent) => {
    event.preventDefault()
    const trimmed = chatInput.trim()
    if (!trimmed) return
    submitRequestMutation.mutate(trimmed)
    setChatInput('')
  }


  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />

      <main className="flex flex-1 gap-4 bg-field-bg p-3.5 pb-20">
        <section className="flex h-full flex-1 flex-col gap-3.5">
          <div className="relative flex-1 overflow-hidden rounded-lg bg-thumbnail">
            <div className="absolute left-4 right-4 top-4 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="rounded-xl bg-live px-2.5 py-1 text-[11px] font-semibold text-white">
                  LIVE
                </span>
                <span className="text-[13px] font-semibold text-white">{STATION_INFO.name}</span>
              </div>
              <div className="flex items-center gap-2 text-[11px] text-nav-inactive">
                <span>{CONNECTION_STATUS_LABEL[connectionStatus]}</span>
                <span>👁 {STATION_INFO.viewerCount}명 시청 중</span>
              </div>
            </div>

            {stream?.error && (
              <div
                role="alert"
                className="absolute inset-x-3.5 top-14 rounded-md bg-danger/90 px-3.5 py-2 text-xs font-semibold text-white"
              >
                ⚠ 방송 송출 문제: {stream.error}
              </div>
            )}

            <button
              type="button"
              className="absolute right-3.5 top-[60px] flex size-8 items-center justify-center rounded-2xl bg-black/45 text-[15px] text-white"
            >
              🔖
            </button>

            <div className="absolute inset-0 flex items-center justify-center">
              <div className="flex size-[120px] items-center justify-center rounded-xl bg-album">
                <span className="text-4xl text-thumbnail-icon">♪</span>
              </div>
            </div>

            <audio ref={audioRef} />
            {playbackError && <p role="alert" className="absolute bottom-36 left-4 text-sm text-red-300">{playbackError}</p>}

            <div className="absolute inset-x-0 bottom-0 flex flex-col gap-1.5 bg-black/35 px-4 py-3.5">
              <p className="text-[11px] text-nav-inactive">공유 라이브 라디오</p>
              <p className="text-base font-semibold text-white">
                {isPlaying ? '라이브 방송 청취 중' : hlsUrl ? '재생 버튼을 눌러 방송에 참여하세요' : '방송 준비 중…'}
              </p>
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={togglePlay}
                  disabled={!hlsUrl}
                  aria-label={isPlaying ? "방송 일시정지" : "방송 재생"}
                  className="flex size-6.5 shrink-0 items-center justify-center rounded-full bg-[#e6e6e6] text-[10px] text-text"
                >
                  {isPlaying ? '⏸' : '▶'}
                </button>
                <div className="h-[5px] flex-1 rounded-full bg-[#737380]">
                  <div
                    className="h-full rounded-full bg-primary"
                    style={{ width: isPlaying ? '100%' : '0%' }}
                  />
                </div>
                <span className="shrink-0 text-[11px] text-nav-inactive">
                  LIVE
                </span>
              </div>
              <p className="text-[11px] text-nav-inactive">
                {broadcastStateQuery.isLoading
                  ? '방송 상태 불러오는 중...'
                  : broadcastStateQuery.isError || !broadcastStateQuery.data
                    ? '방송 상태를 불러오지 못했습니다'
                    : `방송 상태: ${STREAM_STATUS_LABEL[broadcastStateQuery.data.stream.status]}`}
              </p>
            </div>
          </div>

          <div className="flex flex-col gap-2.5 rounded-md border border-border bg-surface px-4 py-3.5">
            <p className="text-sm font-semibold text-text">내 요청 상태</p>
            <div className="flex flex-wrap gap-2.5">
              {MY_REQUEST_STATUSES.map((item) => (
                <span
                  key={item.id}
                  className={`rounded-xl px-2.5 py-1 text-[11px] font-semibold text-white ${STATUS_VARIANT_CLASS[item.variant]}`}
                >
                  {item.text}
                  {item.visibleToSelfOnly ? ' (본인에게만 표시)' : ''}
                </span>
              ))}
            </div>
          </div>
        </section>

        <section className="flex h-full w-[360px] shrink-0 flex-col gap-3 rounded-md border border-border bg-surface p-4">
          <ul className="flex flex-1 flex-col gap-2.5 overflow-y-auto rounded-md bg-field-bg p-3 text-[11px] text-text">
            {[...CHAT_MESSAGES, ...sentMessages].map((message) => (
              <li key={message.id}>
                {message.author}: {message.text}
              </li>
            ))}
          </ul>
          {submitRequestMutation.isPending && (
            <p className="text-[11px] text-text-muted">전송 중...</p>
          )}
          {submitRequestMutation.isError && (
            <p className="text-[11px] text-danger">전송에 실패했어요, 다시 시도해주세요</p>
          )}
          {submitRequestMutation.isSuccess && (
            <p className="text-[11px] text-status-played">사연이 접수됐어요</p>
          )}
          <form onSubmit={handleSendChat} className="flex items-center gap-2">
            <input
              type="text"
              value={chatInput}
              onChange={(event) => setChatInput(event.target.value)}
              className="h-9 flex-1 rounded-full border border-border bg-[#f2f2f2] px-3.5 text-[13px] text-text"
            />
            <button
              type="submit"
              className="flex size-9 shrink-0 items-center justify-center rounded-full bg-primary text-white"
            >
              ➤
            </button>
            <button
              type="button"
              onClick={() => setIsSongRequestOpen(true)}
              aria-label="신청곡"
              className="flex size-9 shrink-0 items-center justify-center rounded-full bg-text-muted text-white"
            >
              🎙
            </button>
          </form>
        </section>
      </main>

      <button
        type="button"
        onClick={() => setIsSurveyOpen(true)}
        className="fixed bottom-8 right-8 flex items-center gap-1.5 rounded-full bg-experiment px-3.5 py-2.5 text-[13px] font-semibold text-white shadow-lg"
      >
        🧪 실험 참여
      </button>

      {isSurveyOpen && <ExperimentSurveyModal onClose={() => setIsSurveyOpen(false)} />}
      {isSongRequestOpen && (
        <SongRequestModal
          onClose={() => setIsSongRequestOpen(false)}
          onSubmitted={(text) =>
            setSentMessages((prev) => [...prev, { id: crypto.randomUUID(), author: '나', text }])
          }
        />
      )}
    </div>
  )
}
