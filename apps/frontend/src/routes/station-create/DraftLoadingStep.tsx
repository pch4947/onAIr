export function DraftLoadingStep() {
  return (
    <div className="flex h-[60vh] flex-col items-center justify-center gap-3">
      <div className="size-10 animate-spin rounded-full border-4 border-field-border border-t-primary" />
      <p className="text-sm font-medium text-text">DJ 캐릭터 초안을 만들고 있어요</p>
      <p className="text-xs text-text-muted">최대 30초 정도 걸릴 수 있어요</p>
    </div>
  )
}
