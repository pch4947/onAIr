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
      <span>{liked ? '❤️' : '🤍'}</span>
      <span>{count}</span>
    </button>
  )
}
