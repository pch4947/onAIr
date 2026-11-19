// 설정 화면 공용 부품 — 화면 설정(#324)과 계정 설정(#325)이 같은 모양을 쓴다
import type { ReactNode } from 'react'

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-1">
      <h2 className="text-xs font-semibold text-text-muted">{title}</h2>
      <div className="flex flex-col divide-y divide-border rounded-md border border-border bg-surface">{children}</div>
    </section>
  )
}

// wide: 슬라이더·입력칸처럼 폭이 필요한 조작은 좁은 화면에서 설명 아래로 내려 전체 폭을 쓴다
export function Row({ label, description, wide = false, children }: {
  label: string; description?: string; wide?: boolean; children: ReactNode
}) {
  return (
    <div className={`flex justify-between gap-4 px-4 py-3.5 ${wide ? 'flex-col gap-3 sm:flex-row sm:items-center' : 'items-center'}`}>
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="text-sm font-medium text-text">{label}</span>
        {description && <span className="text-xs text-text-muted">{description}</span>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

export function Toggle({ label, checked, disabled = false, onChange }: {
  label: string; checked: boolean; disabled?: boolean; onChange: (next: boolean) => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 rounded-full transition-colors disabled:opacity-60 ${checked ? 'bg-primary' : 'bg-field-border'}`}
    >
      <span
        className={`absolute top-0.5 size-5 rounded-full bg-white shadow transition-[left] ${checked ? 'left-5.5' : 'left-0.5'}`}
      />
    </button>
  )
}
