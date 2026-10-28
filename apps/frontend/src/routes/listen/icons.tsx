// public/*.svg 아이콘을 currentColor로 색을 제어할 수 있도록 인라인 SVG로 옮겨온 컴포넌트

interface IconProps {
  className?: string
}

export function EmojiIcon({ className }: IconProps) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="9" />
      <path d="M8.5 14.5c1 1.2 2.2 1.8 3.5 1.8s2.5-.6 3.5-1.8" />
      <path d="M9 9.5h.01" />
      <path d="M15 9.5h.01" />
    </svg>
  )
}

export function MusicNoteIcon({ className }: IconProps) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M9 18V5l11-2v13" />
      <circle cx="6" cy="18" r="3" />
      <circle cx="17" cy="16" r="3" />
    </svg>
  )
}

export function SendIcon({ className }: IconProps) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M21 3L10 14" />
      <path d="M21 3l-7 18-4-7-7-4z" />
    </svg>
  )
}

export function SongRequestAddIcon({ className }: IconProps) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M9 18V5l8-1.5" />
      <circle cx="6" cy="18" r="3" />
      <path d="M19 9v8" />
      <path d="M15 13h8" />
    </svg>
  )
}

export function ReplaySaveIcon({ className }: IconProps) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M12 4v10" />
      <path d="M8 10l4 4 4-4" />
      <path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
    </svg>
  )
}

export function PlayIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor">
      <path d="M20.492,7.969,10.954.975A5,5,0,0,0,3,5.005V19a4.994,4.994,0,0,0,7.954,4.03l9.538-6.994a5,5,0,0,0,0-8.062Z" />
    </svg>
  )
}

export function PauseIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor">
      <path d="M6.5,0A3.5,3.5,0,0,0,3,3.5v17a3.5,3.5,0,0,0,7,0V3.5A3.5,3.5,0,0,0,6.5,0Z" />
      <path d="M17.5,0A3.5,3.5,0,0,0,14,3.5v17a3.5,3.5,0,0,0,7,0V3.5A3.5,3.5,0,0,0,17.5,0Z" />
    </svg>
  )
}

export function ViewIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 512.19 512.19" fill="currentColor">
      <circle cx="256.095" cy="256.095" r="85.333" />
      <path d="M496.543,201.034C463.455,147.146,388.191,56.735,256.095,56.735S48.735,147.146,15.647,201.034c-20.862,33.743-20.862,76.379,0,110.123c33.088,53.888,108.352,144.299,240.448,144.299s207.36-90.411,240.448-144.299C517.405,277.413,517.405,234.777,496.543,201.034z M256.095,384.095c-70.692,0-128-57.308-128-128s57.308-128,128-128s128,57.308,128,128C384.024,326.758,326.758,384.024,256.095,384.095z" />
    </svg>
  )
}

export function VolumeIcon({ className }: IconProps) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor">
      <path d="M20.8,4.293A1,1,0,0,0,19.39,5.707a8.911,8.911,0,0,1,0,12.586A1,1,0,1,0,20.8,19.707,10.911,10.911,0,0,0,20.8,4.293Z" />
      <path d="M18.093,7.293a1,1,0,1,0-1.414,1.414,4.664,4.664,0,0,1,0,6.586,1,1,0,1,0,1.414,1.414A6.665,6.665,0,0,0,18.093,7.293Z" />
      <path d="M13.819.207A12.055,12.055,0,0,0,6.268,5H5a5.006,5.006,0,0,0-5,5v4a5.006,5.006,0,0,0,5,5H6.269a12.051,12.051,0,0,0,7.55,4.793A1,1,0,0,0,15,22.81V1.19A1,1,0,0,0,13.819.207Z" />
    </svg>
  )
}
