// 00b_Main_방송 시작하기 — 선택지 폼 + LLM persona 초안 선택 흐름 (이슈 #66)

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { DraftLoadingStep } from '@/routes/station-create/DraftLoadingStep'
import { DraftSelectStep } from '@/routes/station-create/DraftSelectStep'
import { FormatSelectStep } from '@/routes/station-create/FormatSelectStep'
import { PersonaEditStep } from '@/routes/station-create/PersonaEditStep'
import { PersonaFormStep } from '@/routes/station-create/PersonaFormStep'
import { createStation, generatePersonaDrafts, getStation } from '@/routes/station-create/api'
import { StationApiError } from '@/routes/station-create/client'
import type { StationResult } from '@/routes/station-create/client'
import { randomId } from '@/shared/randomId'
import type { Persona, PersonaForm, StationFormat, StationInfo } from '@/shared/types'

type Step = 'format' | 'form' | 'loading' | 'drafts' | 'edit' | 'submitting' | 'waiting'

interface StationCreateModalProps {
  onClose: () => void
}

export function StationCreateModal({ onClose }: StationCreateModalProps) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [step, setStep] = useState<Step>('format')
  const [selectedFormat, setSelectedFormat] = useState<StationFormat>('talk')
  const [form, setForm] = useState<PersonaForm | null>(null)
  const [stationInfo, setStationInfo] = useState<StationInfo | null>(null)
  const [drafts, setDrafts] = useState<Persona[]>([])
  const [selected, setSelected] = useState<Persona | null>(null)
  const [submitError, setSubmitError] = useState('')
  const [formErrors, setFormErrors] = useState<Record<string, string>>({})
  const [station, setStation] = useState<StationResult | null>(null)
  const [pollError, setPollError] = useState('')
  // 409: 이미 진행 중인 내 방송 — 그 방송으로 안내한다
  const [existingStationId, setExistingStationId] = useState<string | null>(null)
  const retry = useRef<{ body: string; key: string } | null>(null)

  useEffect(() => {
    if (!station || station.status === 'rejected' || (station.status === 'started' && station.hls_ready)) return
    const controller = new AbortController()
    const timer = window.setTimeout(async () => {
      try {
        const result = await getStation(station.station_id, controller.signal)
        if (!controller.signal.aborted) {
          setStation(result)
          setPollError('')
        }
      } catch (error) {
        if (!controller.signal.aborted) setPollError(error instanceof Error ? error.message : '상태를 확인하지 못했어요.')
      }
    }, 2000)
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [station])

  const loadDrafts = async (nextForm: PersonaForm, failureStep: 'form' | 'drafts') => {
    setSubmitError('')
    setFormErrors({})
    setStep('loading')
    try {
      setDrafts(await generatePersonaDrafts(nextForm))
      setStep('drafts')
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : '초안을 만들지 못했어요.')
      if (error instanceof StationApiError && error.errors.length) {
        setFormErrors(Object.fromEntries(error.errors.map((item) => [item.field, item.reason])))
        setStep('form')
      } else setStep(failureStep)
    }
  }

  const handleFormSubmit = async (nextForm: PersonaForm, nextStation: StationInfo) => {
    setForm(nextForm)
    setStationInfo(nextStation)
    await loadDrafts(nextForm, 'form')
  }

  const handleRegenerate = async () => {
    if (!form) return
    await loadDrafts(form, 'drafts')
  }

  const handleSelectDraft = (persona: Persona) => {
    setSelected(persona)
    setSubmitError('')
    setStep('edit')
  }

  const handleConfirm = async (persona: Persona) => {
    if (!stationInfo) return
    setSubmitError('')
    setExistingStationId(null)
    setStep('submitting')
    try {
      const input = { ...stationInfo, persona }
      const body = JSON.stringify(input)
      if (!retry.current || retry.current.body !== body) retry.current = { body, key: randomId() }
      const created = await createStation(input, retry.current.key)
      setStation(created)
      setStep('waiting')
      // 같은 채널 지난 방송의 채팅 캐시가 청취 화면에 잠깐 보이지 않게 버린다
      queryClient.removeQueries({ queryKey: ['requests', created.station_id] })
      // 메인의 방송 목록과 "내 방송으로 가기"를 바로 맞춘다
      void queryClient.invalidateQueries({ queryKey: ['stations'] })
      void queryClient.invalidateQueries({ queryKey: ['my-station'] })
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : '방 만들기에 실패했어요, 잠시 후 다시 시도해주세요')
      if (error instanceof StationApiError && error.status === 409 && error.stationId) setExistingStationId(error.stationId)
      setStep('edit')
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-6">
      <div className="relative w-full max-w-152 rounded-xl bg-surface p-7 shadow-lg">
        <button
          type="button"
          onClick={onClose}
          disabled={step === 'submitting'}
          aria-label="닫기"
          className="absolute right-5 top-5 text-base text-text-muted"
        >
          ×
        </button>

        {step === 'format' && (
          <FormatSelectStep
            initialFormat={selectedFormat}
            onSubmit={(format) => {
              setSelectedFormat(format)
              setStep('form')
            }}
          />
        )}
        <div hidden={step !== 'form'}>
          <PersonaFormStep
            format={selectedFormat}
            onBack={() => setStep('format')}
            onSubmit={handleFormSubmit}
            serverErrors={formErrors}
          />
        </div>
        {(step === 'form' || step === 'drafts') && submitError && <p role="alert" className="text-sm text-danger">{submitError}</p>}
        {step === 'loading' && <DraftLoadingStep />}
        {step === 'drafts' && (
          <DraftSelectStep drafts={drafts} onSelect={handleSelectDraft} onRegenerate={handleRegenerate} />
        )}
        {step === 'waiting' && station && (
          <div className="flex flex-col gap-4" role="status">
            <h2 className="text-lg font-semibold">{station.status === 'rejected' ? '방송을 시작하지 못했어요'
              : station.status === 'started' && station.hls_ready ? '방송이 준비됐어요' : '방송을 준비하고 있어요'}</h2>
            <p className="text-[13px] leading-relaxed text-text-muted">{station.status === 'rejected' ? '선택한 목소리 또는 방송 생성 서비스에 문제가 있어요. 운영자에게 문의해주세요.'
              : station.status === 'preparing' && station.first_song_status === 'preparing'
                ? '첫 노래를 준비하고 있어요. 영상에 따라 몇 분 걸릴 수 있어요.'
              : station.status === 'preparing' ? 'DJ가 방송을 시작할 때까지 기다려주세요.'
              : !station.hls_ready ? 'DJ가 시작됐어요. 재생할 오디오를 준비하고 있어요.' : '청취 화면에서 재생 버튼을 눌러주세요.'}</p>
            {station.first_song_status === 'failed' && station.status !== 'rejected' && (
              <p className="text-xs text-text-muted">첫 노래를 받지 못해서 첫 노래 없이 방송을 시작해요.</p>
            )}
            {pollError && <><p role="alert">{pollError}</p><button type="button" onClick={() => setStation({ ...station })}>상태 다시 확인</button></>}
            {station.status === 'started' && station.hls_ready && <button type="button" onClick={() => { onClose(); navigate(`/listen/${station.station_id}`) }}>방송 듣기</button>}
          </div>
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
        {step === 'edit' && existingStationId && (
          <div className="mt-3 flex justify-end">
            <button
              type="button"
              onClick={() => { onClose(); navigate(`/listen/${existingStationId}`) }}
              className="rounded-md bg-primary px-4.5 py-2.5 text-xs font-semibold text-white"
            >
              내 방송으로 가기
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
