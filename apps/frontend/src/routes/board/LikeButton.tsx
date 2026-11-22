import { useRef } from 'react'

export function HeartIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor">
      <path d="M17.5.917a6.4,6.4,0,0,0-5.5,3.3A6.4,6.4,0,0,0,6.5.917,6.8,6.8,0,0,0,0,7.967c0,6.775,10.956,14.6,11.422,14.932l.578.409.578-.409C13.044,22.569,24,14.742,24,7.967A6.8,6.8,0,0,0,17.5.917Z" />
    </svg>
  )
}

interface LikeButtonProps {
  liked: boolean
  count: number
  // 요청이 끝날 때까지 기다릴 수 있게 Promise를 돌려준다
  onToggle: () => Promise<unknown>
  disabled?: boolean
}

export function LikeButton({ liked, count, onToggle, disabled }: LikeButtonProps) {
  // 요청 중에는 다시 누르지 못한다. disabled는 다시 그려진 뒤에야 걸려서, 빠른 두 번 클릭은 둘 다 통과한다 —
  // 둘 다 같은 상태를 보고 같은 요청을 보낸다 (#366)
  const busy = useRef(false)
  return (
    <button
      type="button"
      aria-pressed={liked}
      disabled={disabled}
      onClick={async (event) => {
        event.stopPropagation()
        if (busy.current) return
        busy.current = true
        try {
          await onToggle()
        } finally {
          busy.current = false
        }
      }}
      className={`flex items-center gap-1 rounded-md px-2 py-1 text-xs font-semibold disabled:opacity-60 ${
        liked ? 'text-live' : 'text-text-muted'
      }`}
    >
      <HeartIcon className="size-3.5" />
      <span>{count}</span>
    </button>
  )
}
