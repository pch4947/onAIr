// logo_letter.png를 마스크로 써서 className의 bg-* 색으로 칠하는 워드마크

interface BrandWordmarkProps {
  className?: string
}

export function BrandWordmark({ className }: BrandWordmarkProps) {
  return (
    <div
      role="img"
      aria-label="onAIr"
      className={`aspect-1199/779 mask-[url(/logo_letter.png)] mask-center mask-no-repeat mask-contain ${className ?? ''}`}
    />
  )
}
