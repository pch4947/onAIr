// 01_Listen_스트리밍 방송 화면

import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Sidebar } from '@/app/Sidebar'
import { useLivePlayback } from '@/player/useLivePlayback'
import { useRealtimeConnection } from '@/realtime/useRealtimeConnection'
import { useRealtimeStore } from '@/realtime/store'
import { bookmarkReplay, getBroadcastState, getRequests, submitRequest } from '@/shared/api'
import { useCurrentUser } from '@/shared/useCurrentUser'
import { ExperimentSurveyModal } from '@/routes/listen/ExperimentSurveyModal'
import { SongRequestModal } from '@/routes/listen/SongRequestModal'
import { SongQueueModal } from '@/routes/listen/SongQueueModal'
import { EmojiIcon, MusicNoteIcon, ReplaySaveIcon, SendIcon, SongRequestAddIcon } from '@/routes/listen/icons'
import { STATUS_VARIANT, STATUS_VARIANT_CLASS, isPendingStatus } from '@/routes/listen/requestStatus'
import { STATION_INFO } from '@/routes/listen/mock'
import type { ConnectionStatus } from '@/realtime/client'
import type { StreamStatus } from '@/shared/types'

const REQUESTS_POLL_INTERVAL_MS = 3000
const CHAT_MAX_LENGTH = 200
const EMOJI_OPTIONS = ['😀', '😂', '😍', '😢', '😮', '👍', '🎉', '🔥', '❤️', '🙏']

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
  const [isEmojiPickerOpen, setIsEmojiPickerOpen] = useState(false)
  const [isQueueOpen, setIsQueueOpen] = useState(false)
  const [chatInput, setChatInput] = useState('')
  useRealtimeConnection()
  const connectionStatus = useRealtimeStore((state) => state.connectionStatus)
  const { data: currentUser } = useCurrentUser()
  const broadcastStateQuery = useQuery({
    queryKey: ['broadcast-state'],
    queryFn: getBroadcastState,
    refetchInterval: BROADCAST_STATE_POLL_INTERVAL_MS,
  })
  const requestsQuery = useQuery({
    queryKey: ['requests'],
    queryFn: getRequests,
    refetchInterval: REQUESTS_POLL_INTERVAL_MS,
  })

  const stream = broadcastStateQuery.data?.stream
  const apiBase = import.meta.env.VITE_API_BASE_URL || window.location.origin
  const hlsPath = import.meta.env.VITE_HLS_URL || stream?.hlsUrl
  const hlsUrl = stream?.status === 'ready' && hlsPath ? new URL(hlsPath, apiBase).href : null
  const { audioRef, isPlaying, error: playbackError, togglePlay } = useLivePlayback(hlsUrl)
  const submitRequestMutation = useMutation({ mutationFn: submitRequest })
  const bookmarkLiveMutation = useMutation({ mutationFn: (sessionId: string) => bookmarkReplay(sessionId) })

  const allRequests = requestsQuery.data?.requests ?? []
  const chatMessages = allRequests.map((item) => ({
    id: item.id,
    author: item.listenerId === currentUser?.id ? '나' : item.listenerId,
    text: item.prompt,
    kind: item.kind,
    status: item.status,
  }))
  const songQueue = allRequests.filter((item) => item.kind === 'mood' && isPendingStatus(item.status))

  const handleSendChat = (event: React.FormEvent) => {
    event.preventDefault()
    const trimmed = chatInput.trim()
    if (!trimmed) return
    submitRequestMutation.mutate({ prompt: trimmed, listenerId: currentUser?.id, kind: 'story' })
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
                <button
                  type="button"
                  onClick={() => stream?.sessionId && bookmarkLiveMutation.mutate(stream.sessionId)}
                  disabled={!stream?.sessionId || bookmarkLiveMutation.isPending}
                  className="flex items-center gap-1 rounded-full bg-black/45 px-2.5 py-1 text-[11px] font-semibold text-white disabled:opacity-50"
                >
                  <ReplaySaveIcon className="size-3.5" />
                  {bookmarkLiveMutation.isPending
                    ? '저장 중...'
                    : bookmarkLiveMutation.isSuccess
                      ? '저장됨'
                      : '다시듣기 저장'}
                </button>
                {bookmarkLiveMutation.isError && (
                  <span className="text-[11px] text-danger">
                    저장 실패: {(bookmarkLiveMutation.error as Error).message}
                  </span>
                )}
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

            <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
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
        </section>

        <section className="flex h-full w-[360px] shrink-0 flex-col gap-3 rounded-md border border-border bg-surface p-4">
          <header className="flex items-center justify-between">
            <p className="text-sm font-semibold text-text">채팅</p>
            <span className="text-[11px] text-text-muted">
              👁 {STATION_INFO.viewerCount.toLocaleString()}
            </span>
          </header>

          <ul className="flex flex-1 flex-col gap-2 overflow-y-auto rounded-md bg-field-bg p-3 text-[11px] text-text">
            {chatMessages.map((message) =>
              message.kind === 'mood' ? (
                <li
                  key={message.id}
                  className="flex flex-col gap-1 rounded-md border border-primary/30 bg-primary/10 px-3 py-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-1 font-semibold text-primary">
                      <MusicNoteIcon className="size-3.5 shrink-0" />
                      {message.author} 님의 신청곡
                    </span>
                    <span
                      className={`shrink-0 rounded-xl px-2 py-0.5 text-[10px] font-semibold text-white ${STATUS_VARIANT_CLASS[STATUS_VARIANT[message.status] ?? 'queued']}`}
                    >
                      {message.status.toUpperCase()}
                    </span>
                  </div>
                  <p className="text-text">{message.text}</p>
                </li>
              ) : (
                <li key={message.id} className="flex items-baseline gap-1.5">
                  <span className="shrink-0 font-semibold text-text-muted">{message.author}</span>
                  <span>{message.text}</span>
                </li>
              ),
            )}
          </ul>
          {submitRequestMutation.isPending && (
            <p className="text-[11px] text-text-muted">전송 중...</p>
          )}
          {submitRequestMutation.isError && (
            <p className="text-[11px] text-danger">전송에 실패했어요, 다시 시도해주세요</p>
          )}
          <div className="relative">
            {isEmojiPickerOpen && (
              <div className="absolute bottom-full left-0 mb-2 grid grid-cols-5 gap-1 rounded-md border border-border bg-surface p-2 shadow-lg">
                {EMOJI_OPTIONS.map((emoji) => (
                  <button
                    key={emoji}
                    type="button"
                    onClick={() => {
                      setChatInput((prev) => (prev + emoji).slice(0, CHAT_MAX_LENGTH))
                      setIsEmojiPickerOpen(false)
                    }}
                    className="flex size-7 items-center justify-center rounded text-base hover:bg-field-bg"
                  >
                    {emoji}
                  </button>
                ))}
              </div>
            )}
            <form
              onSubmit={handleSendChat}
              className="flex items-center gap-2 rounded-lg border border-border bg-[#f2f2f2] px-3 py-1.5"
            >
              <input
                type="text"
                value={chatInput}
                onChange={(event) => setChatInput(event.target.value)}
                maxLength={CHAT_MAX_LENGTH}
                placeholder="메시지 보내기"
                className="h-8 min-w-0 flex-1 bg-transparent text-[13px] text-text placeholder:text-field-placeholder outline-none"
              />
              <button
                type="button"
                onClick={() => setIsEmojiPickerOpen((prev) => !prev)}
                aria-label="이모지"
                className="shrink-0 text-text-muted"
              >
                <EmojiIcon className="size-5" />
              </button>
              <button
                type="button"
                onClick={() => setIsSongRequestOpen(true)}
                aria-label="신청곡"
                className="shrink-0 text-text-muted"
              >
                <SongRequestAddIcon className="size-5" />
              </button>
              <div className="h-5 w-px shrink-0 bg-border" />
              <button
                type="submit"
                className="flex shrink-0 items-center gap-1 text-[13px] font-semibold text-primary"
              >
                <SendIcon className="size-4" />
                전송
              </button>
            </form>
          </div>

          <div className="flex items-center justify-between">
            <button
              type="button"
              onClick={() => setIsQueueOpen(true)}
              className="flex items-center gap-1 rounded-full bg-primary px-2.5 py-1 text-[10px] font-semibold text-white"
            >
              <MusicNoteIcon className="size-3" />
              대기 {songQueue.length}
            </button>
            <span className="text-[10px] text-text-muted">
              {chatInput.length}/{CHAT_MAX_LENGTH}
            </span>
          </div>
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
          listenerId={currentUser?.id}
        />
      )}
      {isQueueOpen && (
        <SongQueueModal
          queue={songQueue}
          currentUserId={currentUser?.id}
          onClose={() => setIsQueueOpen(false)}
        />
      )}
    </div>
  )
}
