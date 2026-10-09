import { useState } from 'react'
import { checkPersonaDraft } from '@/routes/station-create/mock'
import type { Persona } from '@/shared/types'

const fieldLabelClass = 'text-[13px] font-medium text-label'
const textInputClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-[13px] text-text placeholder:text-field-placeholder'
const errorTextClass = 'block text-xs text-danger'

interface ListFieldProps {
  label: string
  items: string[]
  onChange: (items: string[]) => void
  maxItemLength: number
  minItems?: number
  maxItems: number
  errorFor: (index: number) => string | undefined
}

function ListField({ label, items, onChange, maxItemLength, minItems = 0, maxItems, errorFor }: ListFieldProps) {
  return (
    <div className="flex flex-col gap-2">
      <span className={fieldLabelClass}>
        {label} ({items.length}/{maxItems})
      </span>
      {items.map((item, index) => (
        <div key={index} className="flex flex-col gap-1">
          <div className="flex gap-2">
            <input
              type="text"
              value={item}
              maxLength={maxItemLength}
              onChange={(event) => {
                const next = [...items]
                next[index] = event.target.value
                onChange(next)
              }}
              className={textInputClass}
            />
            {items.length > minItems && (
              <button
                type="button"
                onClick={() => onChange(items.filter((_, i) => i !== index))}
                className="shrink-0 rounded-md border border-field-border px-3 text-xs text-text-muted"
              >
                삭제
              </button>
            )}
          </div>
          <span className={errorTextClass}>{errorFor(index) || ' '}</span>
        </div>
      ))}
      {items.length < maxItems && (
        <button
          type="button"
          onClick={() => onChange([...items, ''])}
          className="self-start rounded-md border border-dashed border-field-border px-3.5 py-1.5 text-xs text-text-muted"
        >
          + 추가
        </button>
      )}
    </div>
  )
}

interface PersonaEditStepProps {
  persona: Persona
  isSubmitting: boolean
  submitError: string
  onBack: () => void
  onConfirm: (persona: Persona) => void
}

export function PersonaEditStep({ persona, isSubmitting, submitError, onBack, onConfirm }: PersonaEditStepProps) {
  const [djName, setDjName] = useState(persona.djName)
  const [concept, setConcept] = useState(persona.concept)
  const [tone, setTone] = useState(persona.tone)
  const [examples, setExamples] = useState(persona.examples)
  const [signaturePhrases, setSignaturePhrases] = useState(persona.signaturePhrases)
  const [forbidden, setForbidden] = useState(persona.forbidden)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [isChecking, setIsChecking] = useState(false)

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    const next: Persona = { ...persona, djName, concept, tone, examples, signaturePhrases, forbidden }
    setIsChecking(true)
    const result = await checkPersonaDraft(next)
    setIsChecking(false)
    if (!result.ok) {
      const nextErrors: Record<string, string> = {}
      for (const error of result.errors) nextErrors[error.field] = error.reason
      setFieldErrors(nextErrors)
      return
    }
    setFieldErrors({})
    onConfirm(next)
  }

  return (
    <form onSubmit={handleSubmit} className="flex w-full flex-col gap-5">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold text-text">캐릭터를 다듬어주세요</h1>
        <p className="text-[13px] text-text-muted">방송 중에는 바뀌지 않으니 지금 확인해주세요</p>
      </div>

      <label className="flex flex-col gap-1.5">
        <span className={fieldLabelClass}>DJ 이름</span>
        <input
          type="text"
          value={djName}
          maxLength={20}
          onChange={(event) => setDjName(event.target.value)}
          className={textInputClass}
        />
        <span className={errorTextClass}>{fieldErrors['persona.dj_name'] || ' '}</span>
      </label>

      <label className="flex flex-col gap-1.5">
        <span className={fieldLabelClass}>컨셉</span>
        <input
          type="text"
          value={concept}
          maxLength={60}
          onChange={(event) => setConcept(event.target.value)}
          className={textInputClass}
        />
        <span className={errorTextClass}>{fieldErrors['persona.concept'] || ' '}</span>
      </label>

      <label className="flex flex-col gap-1.5">
        <span className={fieldLabelClass}>말투 묘사</span>
        <input
          type="text"
          value={tone}
          maxLength={100}
          onChange={(event) => setTone(event.target.value)}
          className={textInputClass}
        />
        <span className={errorTextClass}>{fieldErrors['persona.tone'] || ' '}</span>
      </label>

      <ListField
        label="예시 멘트"
        items={examples}
        onChange={setExamples}
        maxItemLength={150}
        minItems={3}
        maxItems={5}
        errorFor={(index) => fieldErrors[`persona.examples.${index}`] || fieldErrors['persona.examples']}
      />

      <ListField
        label="입버릇"
        items={signaturePhrases}
        onChange={setSignaturePhrases}
        maxItemLength={30}
        maxItems={3}
        errorFor={(index) => fieldErrors[`persona.signature_phrases.${index}`]}
      />

      <ListField
        label="금지 사항"
        items={forbidden}
        onChange={setForbidden}
        maxItemLength={50}
        maxItems={10}
        errorFor={(index) => fieldErrors[`persona.forbidden.${index}`]}
      />

      {submitError && <p className="text-xs text-danger">{submitError}</p>}

      <div className="flex gap-2">
        <button
          type="button"
          onClick={onBack}
          className="rounded-md border border-field-border px-4 py-2.5 text-[13px] font-medium text-text-muted"
        >
          다시 고르기
        </button>
        <button
          type="submit"
          disabled={isChecking || isSubmitting}
          className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
        >
          {isChecking ? '확인 중...' : isSubmitting ? '방 만드는 중...' : '방 만들기'}
        </button>
      </div>
    </form>
  )
}
