// 내 채팅·사연·신청곡이 방송에 나간 순간 띄우는 화면 알림 (#324)
import type { AiredToast } from '@/routes/listen/useAiredToasts'

// 아래쪽에 띄운다 — 위쪽은 사이드바의 댓글 알림 자리라 둘이 같이 뜨면 겹치고, 모바일에서는 뒤로가기·방송 이름·
// 채팅 토글 줄을 가린다 (#366). 모바일은 하단 탭과 채팅 입력창 위에 둔다
export function AiredToasts({ toasts, onDismiss }: { toasts: AiredToast[]; onDismiss: (id: string) => void }) {
  if (!toasts.length) return null
  return (
    <div role="status" aria-live="polite" className="fixed inset-x-4 bottom-40 z-50 flex flex-col gap-2 md:inset-x-auto md:bottom-6 md:right-4 md:w-80">
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
