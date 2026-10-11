// 00b_Main_방송 시작하기 — 선택지 폼 + LLM persona 초안 선택 흐름 (이슈 #66)

import { useState } from 'react'
import { DraftLoadingStep } from '@/routes/station-create/DraftLoadingStep'
import { DraftSelectStep } from '@/routes/station-create/DraftSelectStep'
import { PersonaEditStep } from '@/routes/station-create/PersonaEditStep'
import { PersonaFormStep } from '@/routes/station-create/PersonaFormStep'
import { createStationMock, generatePersonaDrafts } from '@/routes/station-create/mock'
import type { Persona, PersonaForm, StationInfo } from '@/shared/types'

type Step = 'form' | 'loading' | 'drafts' | 'edit' | 'submitting'

interface StationCreateModalProps {
  onClose: () => void
}

export function StationCreateModal({ onClose }: StationCreateModalProps) {
  const [step, setStep] = useState<Step>('form')
  const [form, setForm] = useState<PersonaForm | null>(null)
  const [stationInfo, setStationInfo] = useState<StationInfo | null>(null)
  const [drafts, setDrafts] = useState<Persona[]>([])
  const [selected, setSelected] = useState<Persona | null>(null)
  const [submitError, setSubmitError] = useState('')

  const handleFormSubmit = async (nextForm: PersonaForm, nextStation: StationInfo) => {
    setForm(nextForm)
    setStationInfo(nextStation)
    setStep('loading')
    const result = await generatePersonaDrafts(nextForm)
    setDrafts(result)
    setStep('drafts')
  }

  const handleRegenerate = async () => {
    if (!form) return
    setStep('loading')
    const result = await generatePersonaDrafts(form)
    setDrafts(result)
    setStep('drafts')
  }

  const handleSelectDraft = (persona: Persona) => {
    setSelected(persona)
    setStep('edit')
  }

  const handleConfirm = async (persona: Persona) => {
    if (!stationInfo) return
    setSubmitError('')
    setStep('submitting')
    try {
      await createStationMock({
        persona,
        firstSongUrl: stationInfo.firstSongUrl,
        broadcastMinutes: stationInfo.broadcastMinutes,
        topic: stationInfo.topic,
      })
      onClose()
    } catch {
      setSubmitError('방 만들기에 실패했어요, 잠시 후 다시 시도해주세요')
      setStep('edit')
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-6">
      <div className="relative w-full max-w-152 rounded-xl bg-surface p-7 shadow-lg">
        <button
          type="button"
          onClick={onClose}
          className="absolute right-5 top-5 text-base text-text-muted"
        >
          ×
        </button>

        {step === 'form' && <PersonaFormStep onSubmit={handleFormSubmit} />}
        {step === 'loading' && <DraftLoadingStep />}
        {step === 'drafts' && (
          <DraftSelectStep drafts={drafts} onSelect={handleSelectDraft} onRegenerate={handleRegenerate} />
        )}
        {(step === 'edit' || step === 'submitting') && selected && (
          <PersonaEditStep
            persona={selected}
            isSubmitting={step === 'submitting'}
            submitError={submitError}
            onBack={() => setStep('drafts')}
            onConfirm={handleConfirm}
          />
        )}
      </div>
    </div>
  )
}
