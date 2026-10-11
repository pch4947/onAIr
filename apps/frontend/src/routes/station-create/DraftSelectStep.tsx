import type { Persona } from '@/shared/types'

interface DraftSelectStepProps {
  drafts: Persona[]
  onSelect: (persona: Persona) => void
  onRegenerate: () => void
}

export function DraftSelectStep({ drafts, onSelect, onRegenerate }: DraftSelectStepProps) {
  return (
    <div className="flex w-full flex-col gap-5">
      <div className="flex items-center justify-between">
        <div className="flex flex-col gap-1">
          <h1 className="text-[22px] font-semibold text-text">캐릭터 초안을 골라주세요</h1>
          <p className="text-[13px] text-text-muted">마음에 드는 초안을 고르면 다음 단계에서 고칠 수 있어요</p>
        </div>
        <button
          type="button"
          onClick={onRegenerate}
          className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted"
        >
          🔄 다시 생성
        </button>
      </div>

      <ul className="flex flex-wrap gap-4">
        {drafts.map((draft, index) => (
          <li
            key={`${draft.djName}-${index}`}
            className="flex w-[300px] flex-col gap-3 rounded-lg border border-border bg-surface p-5"
          >
            <div className="flex flex-col gap-1">
              <h2 className="text-base font-semibold text-text">{draft.djName}</h2>
              <p className="text-xs text-text-muted">{draft.concept}</p>
            </div>
            <p className="text-xs text-label">{draft.tone}</p>
            <ul className="flex flex-col gap-1.5">
              {draft.examples.slice(0, 3).map((example, exampleIndex) => (
                <li key={exampleIndex} className="rounded-md bg-field-bg px-3 py-2 text-xs text-text">
                  “{example}”
                </li>
              ))}
            </ul>
            {draft.signaturePhrases.length > 0 && (
              <p className="text-[11px] text-text-muted">입버릇: {draft.signaturePhrases.join(', ')}</p>
            )}
            <button
              type="button"
              onClick={() => onSelect(draft)}
              className="mt-auto rounded-md bg-primary py-2.5 text-xs font-semibold text-white"
            >
              이 캐릭터로 선택
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
