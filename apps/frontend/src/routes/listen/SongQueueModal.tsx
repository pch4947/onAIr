import { MusicNoteIcon } from '@/routes/listen/icons'
import type { NowPlayingTrack, QueuedTrack } from '@/shared/types'

interface SongQueueModalProps {
  queue: QueuedTrack[]
  nowPlaying: NowPlayingTrack | null
  onClose: () => void
}

function formatDuration(durationMs: number): string {
  const totalSeconds = Math.floor(durationMs / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

function TrackTitle({ item }: { item: QueuedTrack }) {
  return (
    <div className="flex min-w-0 flex-col">
      <p className="flex min-w-0 items-center gap-1.5">
        <span className="truncate text-[13px] text-text">{item.track.title}</span>
        {item.source === 'dj' && (
          <span className="shrink-0 rounded-xl bg-primary-soft px-1.5 py-0.5 text-[10px] font-semibold text-primary">
            DJ 선곡
          </span>
        )}
      </p>
      {item.track.artist && (
        <p className="truncate text-[11px] text-text-muted">{item.track.artist}</p>
      )}
    </div>
  )
}

export function SongQueueModal({ queue, nowPlaying, onClose }: SongQueueModalProps) {
  const upcoming = queue.filter((item) => item.segment_id !== nowPlaying?.segment_id)
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
      <div className="flex w-full max-w-100 flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg">
        <header className="flex items-center justify-between">
          <p className="flex items-center gap-1.5 text-lg font-semibold text-text">
            <MusicNoteIcon className="size-4.5" />
            음악 대기열 · {upcoming.length}
          </p>
          <button type="button" onClick={onClose} className="text-base text-text-muted">
            ×
          </button>
        </header>

        {nowPlaying && (
          <section aria-label="지금 재생 중" className="flex flex-col gap-1.5">
            <p className="text-[11px] font-semibold text-primary">지금 재생 중</p>
            <div className="flex items-center justify-between gap-3 rounded-md border border-primary/30 bg-primary-soft px-3.5 py-2.5">
              <div className="flex min-w-0 items-center gap-2.5">
                <MusicNoteIcon className="size-3.5 shrink-0 text-primary" />
                <TrackTitle item={nowPlaying} />
              </div>
              <span className="shrink-0 text-[11px] text-text-muted">{formatDuration(nowPlaying.track.duration_ms)}</span>
            </div>
          </section>
        )}

        {upcoming.length === 0 ? (
          <p className="text-[13px] text-text-muted">현재 대기 중인 곡이 없어요</p>
        ) : (
          <ul className="flex max-h-90 flex-col gap-2 overflow-y-auto">
            {upcoming.map((item, index) => (
              <li
                key={item.segment_id}
                className="flex items-center justify-between gap-3 rounded-md bg-field-bg px-3.5 py-2.5"
              >
                <div className="flex min-w-0 items-center gap-2.5">
                  <span className="shrink-0 text-xs font-semibold text-text-muted">{index + 1}</span>
                  <TrackTitle item={item} />
                </div>
                <span className="shrink-0 text-[11px] text-text-muted">{formatDuration(item.track.duration_ms)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
