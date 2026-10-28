import { useState } from 'react'
import { useVodPlayback } from '@/player/useVodPlayback'
import type { ReplaySession } from '@/shared/types'

function formatDuration(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

function formatDateTime(iso: string): string {
  const date = new Date(iso)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}.${pad(date.getMonth() + 1)}.${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

interface ReplayListItemProps {
  replay: ReplaySession
  apiBase: string
  onDelete?: (replay: ReplaySession) => void
}

export function ReplayListItem({ replay, apiBase, onDelete }: ReplayListItemProps) {
  const [isPlaying, setIsPlaying] = useState(false)
  const vodUrl = isPlaying ? new URL(replay.vodUrl, apiBase).href : null
  const { audioRef, error } = useVodPlayback(vodUrl)

  return (
    <li className="flex flex-col gap-2 rounded-md border border-field-border bg-field-bg px-3.5 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex flex-col gap-1">
          <span className="flex items-center gap-1.5 text-[13px] font-medium text-text">
            {formatDateTime(replay.startedAt)}
            {replay.ongoing && (
              <span className="rounded-full bg-live px-3 py-0.5 text-[10px] font-semibold tracking-wide text-white">
                LIVE
              </span>
            )}
          </span>
          <span className="text-[11px] text-text-muted">{formatDuration(replay.durationSeconds)}</span>
        </div>
        <div className="flex shrink-0 items-center gap-2.5">
          <button
            type="button"
            onClick={() => setIsPlaying((prev) => !prev)}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-semibold text-white"
          >
            {isPlaying ? '닫기' : '▶ 재생'}
          </button>
          {onDelete && (
            <button
              type="button"
              onClick={() => onDelete(replay)}
              className="rounded-md bg-danger px-2.5 py-1.5 text-xs font-semibold text-white"
            >
              삭제
            </button>
          )}
        </div>
      </div>
      {isPlaying && <audio ref={audioRef} controls className="w-full" />}
      {isPlaying && error && <p className="text-[11px] text-danger">{error}</p>}
    </li>
  )
}
