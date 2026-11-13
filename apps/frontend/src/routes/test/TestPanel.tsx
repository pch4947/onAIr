// 방송 화면에 붙는 테스트 패널 — 코너·페르소나 변경과, 엔진의 판단 흐름(입력 라우팅·선별·생성·코너·송출)을 보여주는 로그

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getVoices, switchPersona } from '@/routes/station-create/api'
import { StationApiError } from '@/routes/station-create/client'
import { cornerLabel, skippableCorners } from '@/routes/test/corners'
import { PERSONA_PRESETS, presetVoice } from '@/routes/test/presets'
import { TEST_STATION_KEY, testStationQuery } from '@/routes/test/testStation'
import {
  describeEvent, describeInputChange, describePolicy, type LogEntry,
} from '@/routes/test/engineLog'
import { getEngineEvents, getRequests, skipCorner } from '@/shared/api'
import type { BroadcastStateResponse, StationFormat } from '@/shared/types'

const EVENT_POLL_INTERVAL_MS = 2000
const REQUESTS_POLL_INTERVAL_MS = 3000
const MAX_LOG_ENTRIES = 300

const TONE_CLASS: Record<LogEntry['tone'], string> = {
  decision: 'text-primary',
  reflect: 'text-status-played',
  policy: 'text-status-generating',
  input: 'text-text',
  control: 'text-danger',
  muted: 'text-text-muted',
}

const FORMAT_LABEL: Record<StationFormat, string> = { talk: '토크', music: '음악', news: '뉴스' }

const chipClass = (active: boolean) =>
  `rounded-full border px-2.5 py-1 text-[11px] font-semibold disabled:opacity-60 ${
    active ? 'border-primary bg-primary-soft text-primary' : 'border-border bg-surface text-text'
  }`

function clock(at: number) {
  const time = new Date(at * 1000)
  return [time.getHours(), time.getMinutes(), time.getSeconds()].map((part) => String(part).padStart(2, '0')).join(':')
}

export function TestPanel({ state }: { state: BroadcastStateResponse }) {
  const stationId = state.stationId
  const started = state.stationStatus === 'started'
  const currentCorner = state.currentCorner

  const cache = useQueryClient()
  const stationQuery = useQuery(testStationQuery(stationId))
  const format = state.stationFormat ?? stationQuery.data?.config?.format ?? 'talk'
  const corners = skippableCorners(format, stationQuery.data?.config?.corners)
  // 방송 상태가 주기적으로 갱신되므로 전환 결과를 바로 반영한다
  const djName = state.djName ?? stationQuery.data?.config?.persona?.dj_name
  const voicesQuery = useQuery({ queryKey: ['voices'], queryFn: getVoices })

  const [log, setLog] = useState<LogEntry[]>([])
  const push = (entries: LogEntry[]) => {
    if (!entries.length) return
    setLog((prev) => {
      const seen = new Set(prev.map((entry) => entry.id))
      const fresh = entries.filter((entry) => !seen.has(entry.id))
      return fresh.length ? [...prev, ...fresh].sort((a, b) => a.at - b.at).slice(-MAX_LOG_ENTRIES) : prev
    })
  }

  // 엔진 판단 흐름 — 커서를 이어 받아 새 이벤트만 붙인다
  const cursor = useRef<number | null>(null)
  useEffect(() => {
    let stopped = false
    let busy = false
    const poll = async () => {
      if (busy) return
      busy = true
      try {
        const page = await getEngineEvents(stationId, cursor.current)
        if (stopped) return
        cursor.current = page.next
        push(page.events.map(describeEvent))
      } catch {
        // 엔진이 잠깐 안 받아도 다음 주기에 이어 받는다
      } finally {
        busy = false
      }
    }
    void poll()
    const timer = setInterval(() => void poll(), EVENT_POLL_INTERVAL_MS)
    return () => { stopped = true; clearInterval(timer) }
  }, [stationId])

  // 코너가 바뀌면 그 코너가 입력별로 어떻게 대응하는지(정책)를 남긴다
  const lastCorner = useRef<string | null>(null)
  useEffect(() => {
    if (!currentCorner || currentCorner.corner === lastCorner.current) return
    lastCorner.current = currentCorner.corner
    push([describePolicy(currentCorner)])
  }, [currentCorner])

  // 청취자 입력의 상태 변화 — 접수·대기·생성·방송·만료
  const requestsQuery = useQuery({
    queryKey: ['test-requests', stationId], queryFn: () => getRequests(stationId), refetchInterval: REQUESTS_POLL_INTERVAL_MS,
  })
  // 서버가 이번 방송 요청만 준다 (#250). 처음 받은 목록은 이미 지난 일이라 기준으로만 쓴다
  const lastStatus = useRef<Map<string, string> | null>(null)
  useEffect(() => {
    const requests = requestsQuery.data?.requests
    if (!requests) return
    const previous = lastStatus.current
    lastStatus.current = new Map(requests.map((item) => [item.id, item.status]))
    if (!previous) return
    push(requests.filter((item) => previous.get(item.id) !== item.status).map(describeInputChange))
  }, [requestsQuery.data])

  const skipMutation = useMutation({
    mutationFn: (corner: string) => skipCorner(stationId, corner),
    onSuccess: ({ corner, discarded }) => push([{
      id: `skip-${Date.now()}`, at: Date.now() / 1000, tone: 'control',
      title: `코너 변경 요청 → ${cornerLabel(corner)}`,
      detail: `아직 내보내지 않은 멘트 ${discarded}개를 버렸어요. 이미 만든 소리가 나간 뒤 전환돼요 (약 15초)`,
    }]),
  })

  const personaMutation = useMutation({
    mutationFn: (index: number) => {
      const voice = presetVoice(voicesQuery.data, index)
      if (!voice) throw new Error('NO_VOICE')
      return switchPersona(stationId, { ...PERSONA_PRESETS[index].persona, voice })
    },
    onSuccess: ({ dj_name, discarded }) => {
      push([{
        id: `persona-${Date.now()}`, at: Date.now() / 1000, tone: 'control',
        title: `페르소나 전환 요청 → ${dj_name}`,
        detail: `아직 내보내지 않은 멘트 ${discarded}개를 버렸어요. 엔진이 안전 검사 후 새 말투·목소리로 이어가요`,
      }])
      void cache.invalidateQueries({ queryKey: [TEST_STATION_KEY, stationId] })
      void cache.invalidateQueries({ queryKey: ['broadcast-state', stationId] })
    },
  })
  const personaError = personaMutation.error instanceof StationApiError && personaMutation.error.status === 409
    ? '방송 중일 때만 바꿀 수 있어요.' : '페르소나 전환 요청을 보내지 못했어요.'

  // 로그가 바뀌면 맨 아래로 — 위로 올려 지난 로그를 읽는 중이면 그대로 둔다.
  // 줄 수나 마지막 줄로는 못 잡는다: 300줄에서 길이가 멈추고, 늦게 온 결정은 시간순으로 앞에 끼어든다
  const logRef = useRef<HTMLOListElement>(null)
  const followRef = useRef(true)
  useEffect(() => {
    const el = logRef.current
    if (el && followRef.current) el.scrollTop = el.scrollHeight
  }, [log])
  const handleLogScroll = () => {
    const el = logRef.current
    if (el) followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  }

  return (
    <section aria-label="테스트 패널" className="flex min-h-0 flex-1 flex-col gap-3 rounded-md border border-border bg-surface p-3.5 md:h-[44%] md:flex-none">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        {/* 모바일은 로그 자리를 넓히려고 코너 버튼을 한 줄로 두고 옆으로 민다 */}
        <div className="flex max-w-full items-center gap-1.5 overflow-x-auto [scrollbar-width:none] md:flex-wrap [&>*]:shrink-0">
          <h2 className="shrink-0 text-xs font-semibold text-text">코너</h2>
          {corners.map((corner) => {
            const active = corner === currentCorner?.corner
            return (
              <button
                key={corner}
                type="button"
                aria-pressed={active}
                disabled={!started || skipMutation.isPending}
                onClick={() => skipMutation.mutate(corner)}
                className={chipClass(active)}
              >
                {cornerLabel(corner)}
              </button>
            )
          })}
          {skipMutation.isError && <span role="alert" className="text-[11px] text-danger">코너 변경 요청을 보내지 못했어요.</span>}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <h2 className="shrink-0 text-xs font-semibold text-text">페르소나</h2>
          {PERSONA_PRESETS.map((preset, index) => {
            const active = preset.persona.djName === djName
            return (
              <button
                key={preset.id}
                type="button"
                aria-pressed={active}
                disabled={!started || active || personaMutation.isPending || !voicesQuery.data?.length}
                onClick={() => personaMutation.mutate(index)}
                title={preset.description}
                className={chipClass(active)}
              >
                {preset.label}
              </button>
            )
          })}
          {personaMutation.isError && <span role="alert" className="text-[11px] text-danger">{personaError}</span>}
        </div>

        {/* 스테이션 변경은 엔진 지원 전이라 버튼만 둔다 */}
        <div className="flex flex-wrap items-center gap-1.5">
          <h2 className="shrink-0 text-xs font-semibold text-text">스테이션</h2>
          {(Object.keys(FORMAT_LABEL) as StationFormat[]).map((key) => (
            <button key={key} type="button" aria-pressed={key === format} disabled title="준비 중" className={chipClass(key === format)}>
              {FORMAT_LABEL[key]}
            </button>
          ))}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-1.5">
        <h2 className="text-xs font-semibold text-text">엔진 로그</h2>
        <ol ref={logRef} onScroll={handleLogScroll} aria-label="엔진 로그" className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto rounded-md bg-field-bg p-2.5 text-[11px]">
          {log.map((entry) => (
            <li key={entry.id} className="flex gap-2">
              <span className="shrink-0 tabular-nums text-text-muted">{clock(entry.at)}</span>
              <span className="min-w-0">
                <span className={`font-semibold ${TONE_CLASS[entry.tone]}`}>{entry.title}</span>
                {entry.detail && <span className="block whitespace-pre-line text-text-muted">{entry.detail}</span>}
              </span>
            </li>
          ))}
          {log.length === 0 && <li className="text-text-muted">엔진 결정을 기다리는 중…</li>}
        </ol>
      </div>
    </section>
  )
}
