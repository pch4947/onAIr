// 01_Listen_스트리밍 방송 화면

import { useState } from 'react'
import { Sidebar } from '@/app/Sidebar'
import { usePlaybackController } from '@/player/usePlaybackController'
import { ExperimentSurveyModal } from '@/routes/listen/ExperimentSurveyModal'
import {
  CHAT_MESSAGES,
  MY_REQUEST_STATUSES,
  PLAYLIST,
  STATION_INFO,
  SYNC_OFFSET_SEC,
} from '@/routes/listen/mock'
import type { RequestStatusVariant } from '@/routes/listen/mock'

const STATUS_VARIANT_CLASS: Record<RequestStatusVariant, string> = {
  generating: 'bg-status-generating',
  queued: 'bg-status-queued',
  played: 'bg-status-played',
  rejected: 'bg-status-rejected',
  muted: 'bg-status-muted',
}

function formatTime(sec: number): string {
  const minutes = Math.floor(sec / 60)
  const seconds = Math.floor(sec % 60)
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

export function ListenPage() {
  const [isSurveyOpen, setIsSurveyOpen] = useState(false)
  const [chatInput, setChatInput] = useState('')
  const { audioRef, currentTrack, isPlaying, positionSec, togglePlay } =
    usePlaybackController(PLAYLIST)

  const handleSendChat = (event: React.FormEvent) => {
    event.preventDefault()
    setChatInput('')
  }

  const progressPercent = Math.min(100, (positionSec / currentTrack.durationSec) * 100)

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
              <span className="text-[11px] text-nav-inactive">
                👁 {STATION_INFO.viewerCount}명 시청 중
              </span>
            </div>

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

            <div className="absolute inset-x-0 bottom-0 flex flex-col gap-1.5 bg-black/35 px-4 py-3.5">
              <p className="text-[11px] text-nav-inactive">코너: {currentTrack.cornerName}</p>
              <p className="text-base font-semibold text-white">
                지금 재생 중 — {currentTrack.title} · {currentTrack.artist}
              </p>
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={togglePlay}
                  className="flex size-6.5 shrink-0 items-center justify-center rounded-full bg-[#e6e6e6] text-[10px] text-text"
                >
                  {isPlaying ? '⏸' : '▶'}
                </button>
                <div className="h-[5px] flex-1 rounded-full bg-[#737380]">
                  <div
                    className="h-full rounded-full bg-primary"
                    style={{ width: `${progressPercent}%` }}
                  />
                </div>
                <span className="shrink-0 text-[11px] text-nav-inactive">
                  {formatTime(positionSec)} / {formatTime(currentTrack.durationSec)}
                </span>
              </div>
              <p className="text-[11px] text-nav-inactive">
                동기화 오차(E_sync): {SYNC_OFFSET_SEC}s
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
          <p className="text-sm font-semibold text-text">채팅 · 사연</p>
          <ul className="flex flex-1 flex-col gap-2.5 overflow-y-auto rounded-md bg-field-bg p-3 text-[11px] text-text">
            {CHAT_MESSAGES.map((message) => (
              <li key={message.id}>
                {message.author}: {message.text}
              </li>
            ))}
          </ul>
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
    </div>
  )
}
