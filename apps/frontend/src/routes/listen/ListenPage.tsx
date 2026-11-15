// 01_Listen_스트리밍 방송 화면

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Sidebar, type NavKey } from '@/app/Sidebar'
import { useLivePlayback } from '@/player/useLivePlayback'
import { useRealtimeConnection } from '@/realtime/useRealtimeConnection'
import { useRealtimePollInterval, useRealtimeStore } from '@/realtime/store'
import {
  ApiError, bookmarkReplay, getBroadcastState, getMusicQueue, getRequests, sendPresenceHeartbeat, submitRequest,
} from '@/shared/api'
import { useCurrentUser } from '@/shared/useCurrentUser'
import { randomId } from '@/shared/randomId'
import { CornerGuide } from '@/routes/listen/CornerGuide'
import { VoteCard } from '@/routes/listen/VoteCard'
import { isUnauthorizedError, LoginExpiredNotice } from '@/routes/listen/LoginExpiredNotice'
import { MessageModal } from '@/routes/listen/MessageModal'
import { SongRequestModal } from '@/routes/listen/SongRequestModal'
import { SongQueueModal } from '@/routes/listen/SongQueueModal'
import { StationStopButton } from '@/routes/listen/StationStopButton'
import {
  BackIcon, EmojiIcon, MessageIcon, MusicNoteIcon, PauseIcon, PlayIcon, ReplaySaveIcon, SendIcon, SongRequestAddIcon,
  ViewIcon, VolumeIcon,
} from '@/routes/listen/icons'
import { STATUS_VARIANT_CLASS, statusLabel, statusNote, statusVariant } from '@/routes/listen/requestStatus'
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

interface ListenPageProps {
  // 테스트 화면이 같은 방송 화면에 코너 변경·엔진 로그를 끼워 넣는다
  testPanel?: ReactNode
  navActive?: NavKey
  // 테스트 화면처럼 주소에 방송 ID가 없을 때 넘긴다
  stationId?: string
}

export function ListenPage({ testPanel, navActive, stationId: stationIdProp }: ListenPageProps = {}) {
  const navigate = useNavigate()
  // 다중 방송: 주소(/listen/:stationId)의 방송을 듣는다
  const { stationId: routeStationId } = useParams()
  // 라우트(/listen/:stationId)나 테스트 화면이 항상 넘긴다
  const stationId = stationIdProp ?? routeStationId ?? ''
  const [isSongRequestOpen, setIsSongRequestOpen] = useState(false)
  const [isMessageOpen, setIsMessageOpen] = useState(false)
  const [isEmojiPickerOpen, setIsEmojiPickerOpen] = useState(false)
  const [isQueueOpen, setIsQueueOpen] = useState(false)
  const [isVolumeOpen, setIsVolumeOpen] = useState(false)
  const [volume, setVolume] = useState(1)
  const [chatInput, setChatInput] = useState('')
  const [presenceClientId] = useState(getOrCreatePresenceClientId)
  useRealtimeConnection(stationId)
  const connectionStatus = useRealtimeStore((state) => state.connectionStatus)
  const broadcastPollMs = useRealtimePollInterval(BROADCAST_STATE_POLL_INTERVAL_MS)
  const requestsPollMs = useRealtimePollInterval(REQUESTS_POLL_INTERVAL_MS)
  const { data: currentUser } = useCurrentUser()
  const broadcastStateQuery = useQuery({
    queryKey: ['broadcast-state', stationId],
    queryFn: () => getBroadcastState(stationId),
    enabled: Boolean(stationId),
    refetchInterval: broadcastPollMs,
    // 없는 방송(404)은 다시 물어도 없다
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 3,
  })
  const notFound = broadcastStateQuery.error instanceof ApiError && broadcastStateQuery.error.status === 404
  const stationStatus = broadcastStateQuery.data?.stationStatus
  const stationFormat = broadcastStateQuery.data?.stationFormat
  const djName = broadcastStateQuery.data?.djName
  // 음악·뉴스 형식은 엔진이 사연(message)을 방송에 반영하지 않는다
  const canSendMessage = stationFormat !== 'music' && stationFormat !== 'news'
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
    // mine은 사용자마다 다르므로 계정이 바뀌면 이전 사용자의 목록을 다시 쓰지 않는다
    queryKey: ['requests', stationId, currentUser?.id],
    queryFn: () => getRequests(stationId),
    enabled: Boolean(currentUser && stationId),
    refetchInterval: requestsPollMs,
  })
  const musicQueueQuery = useQuery({
    queryKey: ['music-queue', stationId],
    queryFn: () => getMusicQueue(stationId),
    enabled: Boolean(stationId),
    refetchInterval: requestsPollMs,
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

  // 서버가 이번 방송 요청만 준다 (#250)
  const allRequests = requestsQuery.data?.requests ?? []
  const chatMessages = allRequests.map((item) => ({
    id: item.id,
    author: item.mine ? '나' : (item.listenerName || '익명'),
    mine: Boolean(item.mine),
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

  // 모바일에서 채팅을 치는 동안(키보드가 올라온 동안)은 뒤로가기·하단 탭을 숨겨 채팅 칸을 넓힌다 (#308)
  const chatInputRef = useRef<HTMLInputElement>(null)
  const [chatFocused, setChatFocused] = useState(false)
  const blurTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const handleChatFocus = () => {
    if (blurTimer.current) clearTimeout(blurTimer.current)
    setChatFocused(true)
  }
  // 전송·이모지 버튼을 누르는 순간 화면이 먼저 펼쳐지면 버튼 위치가 바뀌어 눌리지 않는다 — 조금 뒤에 펼친다
  const handleChatBlur = () => {
    blurTimer.current = setTimeout(() => setChatFocused(false), 150)
  }
  useEffect(() => () => { if (blurTimer.current) clearTimeout(blurTimer.current) }, [])
  // 키보드가 올라온 동안 실제로 보이는 높이. 아이폰 사파리는 키보드가 올라와도 페이지 높이를 줄이지 않고
  // 화면을 통째로 밀어 올리므로, 이 높이에 화면을 맞춰 채팅 목록이 키보드 위 공간에만 들어가게 한다
  const [keyboardViewport, setKeyboardViewport] = useState<number | null>(null)
  // 안드로이드 뒤로 가기로 키보드만 내리면 입력창은 포커스를 쥔 채 남는다 — 화면 높이가 돌아오면 놓는다
  useEffect(() => {
    const viewport = window.visualViewport
    if (!viewport) return
    let tallest = viewport.height
    let shrunk = false
    const onResize = () => {
      tallest = Math.max(tallest, viewport.height)
      if (viewport.height < tallest * 0.8) {
        shrunk = true
        setKeyboardViewport(Math.round(viewport.height))
        // 사파리가 입력창을 보이려고 밀어 올린 스크롤을 되돌린다 — 화면을 보이는 높이에 맞췄으니 필요 없다
        window.scrollTo(0, 0)
      } else if (shrunk) {
        shrunk = false
        setKeyboardViewport(null)
        if (document.activeElement === chatInputRef.current) chatInputRef.current?.blur()
      }
    }
    viewport.addEventListener('resize', onResize)
    return () => viewport.removeEventListener('resize', onResize)
  }, [])
  const collapsed = chatFocused
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
      stationId, prompt: trimmed, listenerName: currentUser?.name, kind: 'story',
    })
    setChatInput('')
  }



  if (notFound) {
    return (
      <div className="flex h-dvh md:h-screen">
        <Sidebar active={navActive} />
        <main className="flex flex-1 flex-col items-center justify-center gap-4 bg-field-bg p-6 pb-24 text-center">
          <p className="text-sm font-semibold text-text">방송을 찾을 수 없어요</p>
          <p className="text-[13px] text-text-muted">주소가 잘못됐거나 없는 방송이에요.</p>
          <button
            type="button"
            onClick={() => navigate('/main')}
            className="rounded-md bg-primary px-4.5 py-2.5 text-xs font-semibold text-white"
          >
            방송 목록으로
          </button>
        </main>
      </div>
    )
  }

  // 방송 이름·다시듣기 저장 — 데스크톱은 방송 화면 위, 모바일은 뒤로가기 오른쪽에 같은 것을 둔다
  const stationTitle = djName ? `${djName}의 방송` : STATION_INFO.name
  // 테스트 방송은 다시듣기를 남기지 않는다
  const replaySave = !testPanel && (
    <>
      <button
        type="button"
        onClick={() => stream?.sessionId && bookmarkLiveMutation.mutate(stream.sessionId)}
        disabled={!stream?.sessionId || ended || stationStatus === 'cleaning'
          || stationStatus === 'stopping' || bookmarkLiveMutation.isPending}
        className="flex shrink-0 items-center gap-1 rounded-full bg-black/45 px-2.5 py-1 text-[11px] font-semibold text-white disabled:opacity-50"
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
    </>
  )

  return (
    <div
      className="flex h-dvh overflow-hidden md:h-screen"
      style={collapsed && keyboardViewport ? { height: keyboardViewport } : undefined}
    >
      <Sidebar active={navActive} hideMobileNav={collapsed} />

      {/* 채팅 입력 중(모바일)에는 하단 탭이 숨으므로 그 자리만큼 띄우던 아래 여백도 줄인다 */}
      <main className={`flex h-full min-h-0 w-full min-w-0 flex-1 flex-col gap-4 bg-field-bg p-3.5 md:flex-row md:pb-20 ${collapsed ? 'pb-3.5' : 'pb-20'}`}>
        {/* 테스트 패널이 붙으면 모바일은 플레이어를 한 줄로 줄이고 로그·채팅을 반씩 나눠 한 화면에 둔다 */}
        {/* 테스트 화면(모바일): 채팅 입력 중에는 테스트 패널을 숨기고 띠만 남겨 채팅이 키보드 위 공간을 다 쓴다 */}
        <section className={`flex flex-col md:h-full md:flex-1 md:basis-auto ${testPanel ? `min-h-0 gap-2.5 md:gap-3.5 ${collapsed ? 'shrink-0' : 'flex-[3] basis-0'}` : 'shrink-0 gap-3.5'}`}>
          {/* 모바일은 뒤로가기 오른쪽에 방송 이름·다시듣기 저장·방송 종료를 둔다. 채팅 입력 중에는 줄째 숨긴다 */}
          <div className={`flex shrink-0 items-center gap-2 ${collapsed ? 'max-md:hidden' : ''}`}>
            <button
              type="button"
              onClick={() => navigate(-1)}
              aria-label="뒤로가기"
              className="flex w-fit shrink-0 items-center text-text-muted"
            >
              <BackIcon className="size-4.5" />
            </button>
            <div className="flex min-w-0 flex-1 items-center justify-between gap-2 md:hidden">
              <div className="flex min-w-0 items-center gap-2">
                <span className="truncate text-[13px] font-semibold text-text">{stationTitle}</span>
                {replaySave}
              </div>
              <StationStopButton stationId={stationId} userId={currentUser?.id} />
            </div>
          </div>
          {testPanel && (
            <div className="flex shrink-0 items-center gap-3 rounded-lg bg-thumbnail px-4 py-2.5 md:hidden">
              <button
                type="button"
                onClick={togglePlay}
                disabled={!hlsUrl}
                aria-label={isPlaying ? '방송 일시정지' : '방송 재생'}
                className="flex size-6.5 shrink-0 items-center justify-center rounded-full bg-[#e6e6e6] text-text"
              >
                {isPlaying ? <PauseIcon className="size-3" /> : <PlayIcon className="size-3" />}
              </button>
              <div className="h-[5px] flex-1 rounded-full bg-[#737380]">
                <div className="h-full rounded-full bg-primary" style={{ width: isPlaying ? '100%' : '0%' }} />
              </div>
              <span className="shrink-0 text-[11px] text-nav-inactive">{ended ? '종료' : ending ? '종료 중' : 'LIVE'}</span>
            </div>
          )}
          {testPanel && playbackError && <p role="alert" className="text-xs text-danger md:hidden">{playbackError}</p>}
          {/* 모바일은 채팅 칸을 넓히려고 방송 화면 없이 컨트롤 띠(재생·볼륨·재생 바·LIVE·방송 상태)만 둔다 (#308) */}
          <div className={`relative h-auto overflow-hidden rounded-lg bg-thumbnail md:block md:h-full md:flex-1 ${testPanel ? 'hidden' : ''}`}>
            <div className="absolute inset-x-4 top-4 hidden flex-wrap items-center justify-between gap-y-2 md:flex">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-semibold text-white">{stationTitle}</span>
                {replaySave}
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
                className="m-2 rounded-md bg-danger/90 px-3.5 py-2 text-xs font-semibold text-white md:absolute md:inset-x-3.5 md:top-14 md:m-0"
              >
                ⚠ 방송 송출 문제: {stream.error}
              </div>
            )}

            <div className="pointer-events-none absolute inset-0 hidden items-center justify-center md:flex">
              <div className="flex size-20 items-center justify-center rounded-xl bg-album md:size-30">
                <span className="text-2xl text-thumbnail-icon md:text-4xl">♪</span>
              </div>
            </div>

            <audio ref={audioRef} />
            {playbackError && <p role="alert" className="px-4 pt-2 text-sm text-red-300 md:absolute md:bottom-36 md:left-4 md:p-0">{playbackError}</p>}

            <div className="flex flex-col gap-1.5 bg-black/35 px-4 py-3.5 md:absolute md:inset-x-0 md:bottom-0">
              <p className="hidden text-base font-semibold text-white md:block">
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
          {testPanel && <div className={collapsed ? 'hidden md:contents' : 'contents'}>{testPanel}</div>}
        </section>

        <section className={`flex min-h-0 w-full flex-col gap-3 rounded-md border border-border bg-surface p-4 md:h-full md:w-[clamp(300px,28vw,360px)] md:flex-none ${testPanel ? 'flex-[2] basis-0 md:basis-auto' : 'flex-1'}`}>
          <header className="flex items-center justify-between">
            <p className="text-sm font-semibold text-text">채팅</p>
            <div className="flex items-center gap-2">
              {/* 모바일은 신청곡 대기 버튼을 시청자 수 왼쪽에 둔다 (데스크톱은 입력창 아래) */}
              <button
                type="button"
                onClick={() => setIsQueueOpen(true)}
                className="flex items-center gap-1 rounded-full bg-primary px-2.5 py-1 text-[10px] font-semibold text-white md:hidden"
              >
                <MusicNoteIcon className="size-3" />
                대기 {musicQueue.length}
              </button>
              <span className="flex items-center gap-1 text-[11px] text-text-muted">
                <ViewIcon className="size-3" />
                {viewerCount?.toLocaleString() ?? '-'}
              </span>
            </div>
          </header>

          <CornerGuide corner={broadcastStateQuery.data?.currentCorner} stationFormat={stationFormat} />

          {/* 채팅 칸 — 투표가 열리면 칸 안 맨 위에 고정하고, 채팅은 그 아래에서 스크롤된다 */}
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-md bg-field-bg">
            <VoteCard stationId={stationId} userId={currentUser?.id} />

            <ul
              ref={chatListRef}
              onScroll={handleChatScroll}
              className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-3 text-[11px] text-text"
            >
              {chatMessages.map((message) =>
                message.kind === 'song_request' ? (
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
                        className={`shrink-0 rounded-xl px-2 py-0.5 text-[10px] font-semibold text-white ${STATUS_VARIANT_CLASS[statusVariant(message.kind, message.status)]}`}
                      >
                        {statusLabel(message.kind, message.status)}
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
                    {message.mine && statusNote(message.kind, message.status) && (
                      <p className="text-[10px] text-text-muted">{statusNote(message.kind, message.status)}</p>
                    )}
                  </li>
                ) : message.kind === 'message' ? (
                  <li
                    key={message.id}
                    className="flex flex-col gap-1 rounded-md border border-border bg-surface px-3 py-2"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-center gap-1 font-semibold text-text-muted">
                        <MessageIcon className="size-3.5 shrink-0" />
                        <span className="truncate" title={message.author}>{message.author}</span>
                        <span className="shrink-0">님의 사연</span>
                      </span>
                      <span
                        className={`shrink-0 rounded-xl px-2 py-0.5 text-[10px] font-semibold text-white ${STATUS_VARIANT_CLASS[statusVariant(message.kind, message.status)]}`}
                      >
                        {statusLabel(message.kind, message.status)}
                      </span>
                    </div>
                    <p className="wrap-break-word text-text">{message.text}</p>
                    {message.mine && statusNote(message.kind, message.status) && (
                      <p className="text-[10px] text-text-muted">{statusNote(message.kind, message.status)}</p>
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
                    {/* 채팅은 건수가 많아 배지 없이, 내 채팅이 방송에 나왔을 때만 표시한다 (#292) */}
                    {message.mine && message.status === 'reflected' && (
                      <span className="shrink-0 text-[10px] font-semibold text-status-played">방송에 나왔어요</span>
                    )}
                  </li>
                ),
              )}
            </ul>
          </div>
          {submitRequestMutation.isPending && (
            <p className="text-[11px] text-text-muted">전송 중...</p>
          )}
          {submitRequestMutation.isError && (isUnauthorizedError(submitRequestMutation.error)
            ? <LoginExpiredNotice className="text-[11px]" />
            : <p className="text-[11px] text-danger">전송에 실패했어요, 다시 시도해주세요</p>
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
                ref={chatInputRef}
                type="text"
                value={chatInput}
                onFocus={handleChatFocus}
                onBlur={handleChatBlur}
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
              {canSendMessage && (
                <button
                  type="button"
                  onClick={() => setIsMessageOpen(true)}
                  aria-label="사연 보내기"
                  className="shrink-0 text-text-muted"
                >
                  <MessageIcon className="size-5" />
                </button>
              )}
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

          {/* 신청곡 대기·글자 수 줄은 데스크톱만 — 모바일은 대기 버튼이 채팅 머리에 있다 */}
          <div className="hidden items-center justify-between md:flex">
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

      {isSongRequestOpen && (
        <SongRequestModal
          onClose={() => setIsSongRequestOpen(false)}
          stationId={stationId}
          onSubmitted={() => { void requestsQuery.refetch(); void musicQueueQuery.refetch() }}
        />
      )}
      {/* 방송 중 형식이 음악·뉴스로 바뀌면 열려 있던 창도 닫는다 — 보내도 방송에 반영되지 않는다 */}
      {isMessageOpen && canSendMessage && (
        <MessageModal
          stationId={stationId}
          listenerName={currentUser?.name}
          onClose={() => setIsMessageOpen(false)}
          onSubmitted={() => { void requestsQuery.refetch() }}
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
