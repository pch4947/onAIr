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
  onToggle: () => void
  disabled?: boolean
}

export function LikeButton({ liked, count, onToggle, disabled }: LikeButtonProps) {
  return (
    <button
      type="button"
      aria-pressed={liked}
      disabled={disabled}
      onClick={(event) => {
        event.stopPropagation()
        onToggle()
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
