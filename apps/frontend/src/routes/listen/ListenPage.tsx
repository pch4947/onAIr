// 01_Listen_스트리밍 방송 화면

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Sidebar } from '@/app/Sidebar'
import { useLivePlayback } from '@/player/useLivePlayback'
import { useRealtimeConnection } from '@/realtime/useRealtimeConnection'
import { useRealtimeStore } from '@/realtime/store'
import {
  bookmarkReplay, getBroadcastState, getMusicQueue, getRequests, sendPresenceHeartbeat, submitRequest,
} from '@/shared/api'
import { useCurrentUser } from '@/shared/useCurrentUser'
import { randomId } from '@/shared/randomId'
import { ExperimentSurveyModal } from '@/routes/listen/ExperimentSurveyModal'
import { SongRequestModal } from '@/routes/listen/SongRequestModal'
import { SongQueueModal } from '@/routes/listen/SongQueueModal'
import { StationStopButton } from '@/routes/listen/StationStopButton'
import {
  BackIcon, EmojiIcon, MusicNoteIcon, PauseIcon, PlayIcon, ReplaySaveIcon, SendIcon, SongRequestAddIcon, ViewIcon,
  VolumeIcon,
} from '@/routes/listen/icons'
import { STATUS_VARIANT, STATUS_VARIANT_CLASS } from '@/routes/listen/requestStatus'
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
const PRESENCE_HEARTBEAT_INTERVAL_MS = 15000
const PRESENCE_CLIENT_ID_KEY = 'onair_presence_client_id'

function getOrCreatePresenceClientId(): string {
  try {
    const existing = sessionStorage.getItem(PRESENCE_CLIENT_ID_KEY)
    if (existing) return existing
    const generated = randomId()
    sessionStorage.setItem(PRESENCE_CLIENT_ID_KEY, generated)
    return generated
  } catch {
    return randomId()
  }
}

export function ListenPage() {
  const navigate = useNavigate()
  const [isSurveyOpen, setIsSurveyOpen] = useState(false)
  const [isSongRequestOpen, setIsSongRequestOpen] = useState(false)
  const [isEmojiPickerOpen, setIsEmojiPickerOpen] = useState(false)
  const [isQueueOpen, setIsQueueOpen] = useState(false)
  const [isVolumeOpen, setIsVolumeOpen] = useState(false)
  const [volume, setVolume] = useState(1)
  const [chatInput, setChatInput] = useState('')
  const [presenceClientId] = useState(getOrCreatePresenceClientId)
  useRealtimeConnection()
  const connectionStatus = useRealtimeStore((state) => state.connectionStatus)
  const { data: currentUser } = useCurrentUser()
  const broadcastStateQuery = useQuery({
    queryKey: ['broadcast-state'],
    queryFn: getBroadcastState,
    refetchInterval: BROADCAST_STATE_POLL_INTERVAL_MS,
  })
  const stationId = broadcastStateQuery.data?.stationId
  const stationStatus = broadcastStateQuery.data?.stationStatus
  const ended = stationStatus === 'ended' || stationStatus === 'rejected'
  const ending = ['stopping', 'cleaning', 'draining'].includes(stationStatus ?? '')

  useEffect(() => {
    if (!stationId) return
    sendPresenceHeartbeat(stationId, presenceClientId).catch(() => {})
    const timer = setInterval(() => {
      sendPresenceHeartbeat(stationId, presenceClientId).catch(() => {})
    }, PRESENCE_HEARTBEAT_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [stationId, presenceClientId])
  const requestsQuery = useQuery({
    queryKey: ['requests'],
    queryFn: getRequests,
    refetchInterval: REQUESTS_POLL_INTERVAL_MS,
  })
  const musicQueueQuery = useQuery({
    queryKey: ['music-queue', stationId],
    queryFn: () => getMusicQueue(stationId!),
    enabled: Boolean(stationId),
    refetchInterval: REQUESTS_POLL_INTERVAL_MS,
  })

  const stream = broadcastStateQuery.data?.stream
  const viewerCount = broadcastStateQuery.data?.viewerCount
  const apiBase = import.meta.env.VITE_API_BASE_URL || window.location.origin
  const hlsPath = import.meta.env.VITE_HLS_URL || stream?.hlsUrl
  const hlsUrl = stream?.status === 'ready' && hlsPath ? new URL(hlsPath, apiBase).href : null
  const { audioRef, isPlaying, error: playbackError, togglePlay } = useLivePlayback(hlsUrl)

  useEffect(() => {
    if (audioRef.current) audioRef.current.volume = volume
  }, [audioRef, volume])

  const submitRequestMutation = useMutation({ mutationFn: submitRequest })
  const bookmarkLiveMutation = useMutation({ mutationFn: (sessionId: string) => bookmarkReplay(sessionId) })

  const allRequests = requestsQuery.data?.requests ?? []
  const chatMessages = allRequests.map((item) => ({
    id: item.id,
    author: item.listenerId === currentUser?.id ? '나' : (item.listenerName || '익명'),
    text: item.prompt,
    kind: item.kind,
    status: item.status,
    trackTitle: item.trackTitle,
    trackArtist: item.trackArtist,
    trackThumbnailUrl: item.trackThumbnailUrl,
  }))
  const musicQueue = musicQueueQuery.data?.queue ?? []
  const playingSegmentId = musicQueueQuery.data?.playingSegmentId ?? null

  const chatListRef = useRef<HTMLUListElement>(null)
  const isNearBottomRef = useRef(true)

  const handleChatScroll = () => {
    const el = chatListRef.current
    if (!el) return
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  useEffect(() => {
    const el = chatListRef.current
    if (el && isNearBottomRef.current) {
      el.scrollTop = el.scrollHeight
    }
  }, [chatMessages.length])

  const handleSendChat = (event: React.FormEvent) => {
    event.preventDefault()
    const trimmed = chatInput.trim()
    if (!trimmed) return
    submitRequestMutation.mutate({
      prompt: trimmed, listenerId: currentUser?.id, listenerName: currentUser?.name, kind: 'story',
    })
    setChatInput('')
  }


  return (
    <div className="flex h-dvh overflow-hidden md:h-screen">
      <Sidebar />

      <main className="flex h-full min-h-0 w-full min-w-0 flex-1 flex-col gap-4 bg-field-bg p-3.5 pb-20 md:flex-row">
        <section className="flex shrink-0 flex-col gap-3.5 md:h-full md:flex-1">
          <button
            type="button"
            onClick={() => navigate(-1)}
            aria-label="뒤로가기"
            className="flex w-fit shrink-0 items-center text-text-muted"
          >
            <BackIcon className="size-4.5" />
          </button>
          <div className="relative h-72 overflow-hidden rounded-lg bg-thumbnail md:h-full md:flex-1">
            <div className="absolute inset-x-4 top-4 flex flex-wrap items-center justify-between gap-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-semibold text-white">{STATION_INFO.name}</span>
                <button
                  type="button"
                  onClick={() => stream?.sessionId && bookmarkLiveMutation.mutate(stream.sessionId)}
                  disabled={!stream?.sessionId || ended || stationStatus === 'cleaning'
                    || stationStatus === 'stopping' || bookmarkLiveMutation.isPending}
                  className="flex items-center gap-1 rounded-full bg-black/45 px-2.5 py-1 text-[11px] font-semibold text-white disabled:opacity-50"
                >
                  <ReplaySaveIcon className="size-3.5" />
                  <span className="hidden sm:inline">
                    {bookmarkLiveMutation.isPending
                      ? '저장 중...'
                      : bookmarkLiveMutation.isSuccess
                        ? '저장됨'
                        : '다시듣기 저장'}
                  </span>
                </button>
                {bookmarkLiveMutation.isError && (
                  <span className="text-[11px] text-danger">
                    저장 실패: {(bookmarkLiveMutation.error as Error).message}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2 text-[11px] text-nav-inactive">
                <StationStopButton stationId={stationId} userId={currentUser?.id} />
                <span className="hidden sm:inline">{CONNECTION_STATUS_LABEL[connectionStatus]}</span>
                <span className="flex items-center gap-1">
                  <ViewIcon className="size-3" />
                  {viewerCount ?? '-'}명 시청 중
                </span>
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
              <div className="flex size-20 items-center justify-center rounded-xl bg-album md:size-30">
                <span className="text-2xl text-thumbnail-icon md:text-4xl">♪</span>
              </div>
            </div>

            <audio ref={audioRef} />
            {playbackError && <p role="alert" className="absolute bottom-36 left-4 text-sm text-red-300">{playbackError}</p>}

            <div className="absolute inset-x-0 bottom-0 flex flex-col gap-1.5 bg-black/35 px-4 py-3.5">
              <p className="text-base font-semibold text-white">
                {ended ? '방송이 종료되었습니다' : ending ? '방송 종료 후 정리 중입니다' : isPlaying ? '라이브 방송 청취 중' : hlsUrl ? '' : '방송 준비 중…'}
              </p>
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={togglePlay}
                  disabled={!hlsUrl}
                  aria-label={isPlaying ? "방송 일시정지" : "방송 재생"}
                  className="flex size-6.5 shrink-0 items-center justify-center rounded-full bg-[#e6e6e6] text-text"
                >
                  {isPlaying ? <PauseIcon className="size-3" /> : <PlayIcon className="size-3" />}
                </button>
                <div className="relative shrink-0">
                  {isVolumeOpen && (
                    <div className="absolute bottom-full left-1/2 mb-2 -translate-x-1/2 rounded-md bg-black/80 px-2 py-3">
                      <input
                        type="range"
                        min={0}
                        max={1}
                        step={0.01}
                        value={volume}
                        onChange={(event) => setVolume(Number(event.target.value))}
                        className="h-20 w-1 accent-primary"
                        style={{ writingMode: 'vertical-lr', direction: 'rtl' }}
                        aria-label="볼륨 조절"
                      />
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={() => setIsVolumeOpen((prev) => !prev)}
                    aria-label="볼륨 조절 열기"
                    className="flex size-6.5 shrink-0 items-center justify-center rounded-full bg-[#e6e6e6] text-text"
                  >
                    <VolumeIcon className="size-3" />
                  </button>
                </div>
                <div className="h-[5px] flex-1 rounded-full bg-[#737380]">
                  <div
                    className="h-full rounded-full bg-primary"
                    style={{ width: isPlaying ? '100%' : '0%' }}
                  />
                </div>
                <span className="shrink-0 text-[11px] text-nav-inactive">
                  {ended ? '종료' : ending ? '종료 중' : 'LIVE'}
                </span>
              </div>
              <p className="text-[11px] text-nav-inactive">
                {broadcastStateQuery.isLoading
                  ? '방송 상태 불러오는 중...'
                  : broadcastStateQuery.isError || !broadcastStateQuery.data
                    ? '방송 상태를 불러오지 못했습니다'
                    : `방송 상태: ${ended ? '종료됨' : ending ? '종료·정리 중' : STREAM_STATUS_LABEL[broadcastStateQuery.data.stream.status]}`}
              </p>
            </div>
          </div>
        </section>

        <section className="flex min-h-0 w-full flex-1 flex-col gap-3 rounded-md border border-border bg-surface p-4 md:h-full md:w-[clamp(300px,28vw,360px)] md:flex-none">
          <header className="flex items-center justify-between">
            <p className="text-sm font-semibold text-text">채팅</p>
            <span className="flex items-center gap-1 text-[11px] text-text-muted">
              <ViewIcon className="size-3" />
              {viewerCount?.toLocaleString() ?? '-'}
            </span>
          </header>

          <ul
            ref={chatListRef}
            onScroll={handleChatScroll}
            className="flex flex-1 flex-col gap-2 overflow-y-auto rounded-md bg-field-bg p-3 text-[11px] text-text"
          >
            {chatMessages.map((message) =>
              message.kind === 'mood' || message.kind === 'song_request' ? (
                <li
                  key={message.id}
                  className="flex flex-col gap-1 rounded-md border border-primary/30 bg-primary/10 px-3 py-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex min-w-0 items-center gap-1 font-semibold text-primary">
                      <MusicNoteIcon className="size-3.5 shrink-0" />
                      <span className="truncate" title={message.author}>{message.author}</span>
                      <span className="shrink-0">님의 신청곡</span>
                    </span>
                    <span
                      className={`shrink-0 rounded-xl px-2 py-0.5 text-[10px] font-semibold text-white ${STATUS_VARIANT_CLASS[message.kind === 'song_request' && message.status === 'published' ? 'queued' : STATUS_VARIANT[message.status] ?? 'queued']}`}
                    >
                      {message.status.toUpperCase()}
                    </span>
                  </div>
                  {message.trackTitle ? (
                    <div className="flex items-center gap-2">
                      {message.trackThumbnailUrl && (
                        <img
                          src={message.trackThumbnailUrl}
                          alt=""
                          referrerPolicy="no-referrer"
                          className="size-10 shrink-0 rounded object-cover"
                        />
                      )}
                      <div className="min-w-0">
                        <p className="truncate text-text">{message.trackTitle}</p>
                        {message.trackArtist && (
                          <p className="truncate text-[10px] text-text-muted">{message.trackArtist}</p>
                        )}
                      </div>
                    </div>
                  ) : (
                    <p className="text-text">{message.text}</p>
                  )}
                </li>
              ) : (
                <li key={message.id} className="flex items-baseline gap-1.5">
                  <span
                    className="max-w-18 shrink-0 truncate font-semibold text-text-muted"
                    title={message.author}
                  >
                    {message.author}
                  </span>
                  <span className="min-w-0 flex-1 wrap-break-word">{message.text}</span>
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
                className="h-8 min-w-0 flex-1 bg-transparent text-base text-text placeholder:text-field-placeholder outline-none md:text-[13px]"
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
              대기 {musicQueue.length}
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
        className="fixed bottom-20 right-4 flex items-center gap-1.5 rounded-full bg-experiment px-3.5 py-2.5 text-[13px] font-semibold text-white shadow-lg md:bottom-8 md:right-8"
      >
        🧪 실험 참여
      </button>

      {isSurveyOpen && <ExperimentSurveyModal onClose={() => setIsSurveyOpen(false)} />}
      {isSongRequestOpen && (
        <SongRequestModal
          onClose={() => setIsSongRequestOpen(false)}
          stationId={stationId}
          onSubmitted={() => { void requestsQuery.refetch(); void musicQueueQuery.refetch() }}
        />
      )}
      {isQueueOpen && (
        <SongQueueModal
          queue={musicQueue}
          playingSegmentId={playingSegmentId}
          onClose={() => setIsQueueOpen(false)}
        />
      )}
    </div>
  )
}
