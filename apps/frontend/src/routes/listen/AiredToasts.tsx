// 내 채팅·사연·신청곡이 방송에 나간 순간 띄우는 화면 알림 (#324)
import type { AiredToast } from '@/routes/listen/useAiredToasts'

export function AiredToasts({ toasts, onDismiss }: { toasts: AiredToast[]; onDismiss: (id: string) => void }) {
  if (!toasts.length) return null
  return (
    <div role="status" aria-live="polite" className="fixed inset-x-4 top-4 z-50 flex flex-col gap-2 md:inset-x-auto md:right-4 md:w-80">
      {toasts.map((toast) => (
        <button
          key={toast.id}
          type="button"
          onClick={() => onDismiss(toast.id)}
          className="flex flex-col gap-1 rounded-md border border-primary-soft-border bg-primary-soft px-4 py-3.5 text-left shadow-md"
        >
          <p className="text-sm font-semibold text-[#1a1a1f]">{toast.title}</p>
          <p className="truncate text-xs text-text-muted">{toast.detail}</p>
        </button>
      ))}
    </div>
  )
}
