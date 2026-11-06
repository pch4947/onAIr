// 코너·페르소나 테스트 — 방송이 하나뿐이라(#161 전) 한 번에 한 조합만 띄운다

import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Sidebar } from '@/app/Sidebar'
import { createStation, getVoices } from '@/routes/station-create/api'
import { StationApiError } from '@/routes/station-create/client'
import { CORNER_PRESETS, PERSONA_PRESETS } from '@/routes/test/presets'
import { getBroadcastState } from '@/shared/api'
import { randomId } from '@/shared/randomId'
import { useCurrentUser } from '@/shared/useCurrentUser'

const BROADCAST_STATE_POLL_INTERVAL_MS = 3000
const TEST_BROADCAST_MINUTES = 30
const BUSY_STATUSES = new Set(['preparing', 'started', 'draining', 'stopping', 'cleaning'])

const selectClass =
  'h-10 w-full rounded-lg border border-field-border bg-field-bg px-3.5 text-base text-text md:max-w-80 md:text-[13px]'

const optionClass = (selected: boolean) =>
  `flex flex-col gap-1 rounded-lg border px-4 py-3 text-left ${
    selected ? 'border-primary bg-primary-soft' : 'border-border bg-surface'
  }`

export function TestPage() {
  const navigate = useNavigate()
  const { data: currentUser } = useCurrentUser()
  const [cornerId, setCornerId] = useState(CORNER_PRESETS[0].id)
  const [personaId, setPersonaId] = useState(PERSONA_PRESETS[0].id)
  const [voiceId, setVoiceId] = useState('')
  const broadcastStateQuery = useQuery({
    queryKey: ['broadcast-state'],
    queryFn: getBroadcastState,
    refetchInterval: BROADCAST_STATE_POLL_INTERVAL_MS,
  })
  const voicesQuery = useQuery({ queryKey: ['voices'], queryFn: getVoices })
  const voices = voicesQuery.data ?? []
  const selectedVoice = voiceId || voices[0]?.id || ''
  const stationStatus = broadcastStateQuery.data?.stationStatus
  const busy = BUSY_STATUSES.has(stationStatus ?? '')

  // 프리셋 목록이 바뀌어 예전에 고른 id가 없어지면 첫 항목으로 돌아간다
  const corner = CORNER_PRESETS.find((preset) => preset.id === cornerId) ?? CORNER_PRESETS[0]
  const personaPreset = PERSONA_PRESETS.find((preset) => preset.id === personaId) ?? PERSONA_PRESETS[0]

  const startMutation = useMutation({
    mutationFn: () => {
      const persona = personaPreset.persona
      return createStation({
        broadcastMinutes: TEST_BROADCAST_MINUTES, format: corner.format, corners: corner.corners, intensity: 'mid',
        persona: { ...persona, voice: selectedVoice },
      }, randomId())
    },
    onSuccess: () => navigate('/listen'),
  })

  // 테스트 화면은 어드민만 쓴다. 메뉴는 숨겼지만 주소로 직접 들어와도 돌려보낸다
  if (currentUser && !currentUser.isAdmin) {
    return <Navigate to="/main" replace />
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar active="test" />

      <main className="flex flex-1 flex-col gap-6 bg-bg p-4 pb-24 md:p-8 md:pb-8">
        <header className="flex flex-col gap-1">
          <h1 className="text-center text-lg font-semibold text-text md:text-left md:text-[22px]">테스트</h1>
          <p className="text-center text-[13px] text-text-muted md:text-left">
            코너 조합과 페르소나를 골라 바로 방송을 시작하고, 채팅·사연·신청곡이 어떻게 반영되는지 확인해요.
          </p>
        </header>

        <section className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold text-text">형식 · 코너</h2>
          <select
            value={corner.id}
            onChange={(event) => setCornerId(event.target.value)}
            aria-label="형식 · 코너"
            className={selectClass}
          >
            {CORNER_PRESETS.map((preset) => (
              <option key={preset.id} value={preset.id}>{preset.label}</option>
            ))}
          </select>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold text-text">페르소나</h2>
          <div className="grid gap-2.5 md:grid-cols-2">
            {PERSONA_PRESETS.map((preset) => (
              <button
                key={preset.id}
                type="button"
                aria-pressed={personaPreset.id === preset.id}
                onClick={() => setPersonaId(preset.id)}
                className={optionClass(personaPreset.id === preset.id)}
              >
                <span className="text-[13px] font-semibold text-text">{preset.label}</span>
                <span className="text-xs text-text-muted">{preset.description}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold text-text">목소리</h2>
          <select
            value={selectedVoice}
            onChange={(event) => setVoiceId(event.target.value)}
            disabled={voices.length === 0}
            aria-label="목소리"
            className={selectClass}
          >
            {voices.map((voice) => (
              <option key={voice.id} value={voice.id}>{voice.name}</option>
            ))}
          </select>
          {voicesQuery.isError && <p className="text-xs text-danger">목소리 목록을 불러오지 못했어요.</p>}
        </section>

        <div className="flex flex-col items-end gap-2">
          {startMutation.isError && (
            <p className="text-xs text-danger">
              {startMutation.error instanceof StationApiError ? startMutation.error.message : '방송을 시작하지 못했어요. 잠시 후 다시 시도해주세요.'}
            </p>
          )}
          <button
            type="button"
            onClick={() => startMutation.mutate()}
            disabled={busy || !selectedVoice || startMutation.isPending}
            className="rounded-md bg-primary px-5 py-2.5 text-[13px] font-semibold text-white disabled:opacity-60"
          >
            {startMutation.isPending ? '방송 만드는 중...' : '테스트 방송 시작하기'}
          </button>
        </div>
      </main>
    </div>
  )
}
