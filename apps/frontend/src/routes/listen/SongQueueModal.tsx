import { MusicNoteIcon } from '@/routes/listen/icons'
import { STATUS_VARIANT, STATUS_VARIANT_CLASS } from '@/routes/listen/requestStatus'
import type { RequestRecord } from '@/shared/types'

interface SongQueueModalProps {
  queue: RequestRecord[]
  currentUserId?: string
  onClose: () => void
}

export function SongQueueModal({ queue, currentUserId, onClose }: SongQueueModalProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
      <div className="flex w-full max-w-100 flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg">
        <header className="flex items-center justify-between">
          <p className="flex items-center gap-1.5 text-lg font-semibold text-text">
            <MusicNoteIcon className="size-4.5" />
            신청곡 대기열 · {queue.length}
          </p>
          <button type="button" onClick={onClose} className="text-base text-text-muted">
            ×
          </button>
        </header>

        {queue.length === 0 ? (
          <p className="text-[13px] text-text-muted">현재 대기 중인 신청곡이 없어요</p>
        ) : (
          <ul className="flex max-h-90 flex-col gap-2 overflow-y-auto">
            {queue.map((item, index) => (
              <li
                key={item.id}
                className="flex items-center justify-between gap-3 rounded-md bg-field-bg px-3.5 py-2.5"
              >
                <div className="flex min-w-0 items-center gap-2.5">
                  <span className="shrink-0 text-xs font-semibold text-text-muted">{index + 1}</span>
                  <div className="flex min-w-0 flex-col">
                    <p className="truncate text-[13px] text-text">{item.prompt}</p>
                    <p className="text-[11px] text-text-muted">
                      {item.listenerId === currentUserId ? '나' : item.listenerId}
                    </p>
                  </div>
                </div>
                <span
                  className={`shrink-0 rounded-xl px-2 py-0.5 text-[10px] font-semibold text-white ${STATUS_VARIANT_CLASS[STATUS_VARIANT[item.status] ?? 'queued']}`}
                >
                  {item.status.toUpperCase()}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
