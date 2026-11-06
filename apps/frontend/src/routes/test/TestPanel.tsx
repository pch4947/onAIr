// 방송 화면에 붙는 테스트 패널 — 코너 변경과, 엔진이 입력을 어떻게 분류하고 무엇을 송출하기로 했는지 보여주는 로그

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { cornerLabel, skippableCorners } from '@/routes/test/corners'
import { PERSONA_PRESETS } from '@/routes/test/presets'
import { testStationQuery } from '@/routes/test/testStation'
import {
  describeDecision, describeInputChange, describePolicy, type LogEntry,
} from '@/routes/test/engineLog'
import { getDecisions, getRequests, skipCorner } from '@/shared/api'
import type { BroadcastStateResponse, StationFormat } from '@/shared/types'

const DECISION_POLL_INTERVAL_MS = 2000
const REQUESTS_POLL_INTERVAL_MS = 3000
const MAX_LOG_ENTRIES = 300

const TONE_CLASS: Record<LogEntry['tone'], string> = {
  decision: 'text-primary',
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

  const stationQuery = useQuery(testStationQuery(stationId))
  const format = state.stationFormat ?? stationQuery.data?.config?.format ?? 'talk'
  const corners = skippableCorners(format, stationQuery.data?.config?.corners)
  const djName = stationQuery.data?.config?.persona?.dj_name

  const [log, setLog] = useState<LogEntry[]>([])
  const push = (entries: LogEntry[]) => {
    if (!entries.length) return
    setLog((prev) => {
      const seen = new Set(prev.map((entry) => entry.id))
      const fresh = entries.filter((entry) => !seen.has(entry.id))
      return fresh.length ? [...prev, ...fresh].sort((a, b) => a.at - b.at).slice(-MAX_LOG_ENTRIES) : prev
    })
  }

  // 엔진 결정 — 커서를 이어 받아 새 결정만 붙인다
  const cursor = useRef<number | null>(null)
  useEffect(() => {
    let stopped = false
    let busy = false
    const poll = async () => {
      if (busy) return
      busy = true
      try {
        const page = await getDecisions(stationId, cursor.current)
        if (stopped) return
        cursor.current = page.next
        push(page.decisions.map(describeDecision))
      } catch {
        // 엔진이 잠깐 안 받아도 다음 주기에 이어 받는다
      } finally {
        busy = false
      }
    }
    void poll()
    const timer = setInterval(() => void poll(), DECISION_POLL_INTERVAL_MS)
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
  const requestsQuery = useQuery({ queryKey: ['requests'], queryFn: getRequests, refetchInterval: REQUESTS_POLL_INTERVAL_MS })
  const lastStatus = useRef<Map<string, string> | null>(null)
  const lastSince = useRef<number | null>(null)
  // 지난 방송 입력은 방송이 끝날 때 실패·만료로 바뀌는데, 이번 방송 로그에 섞이지 않게 시작 뒤 입력만 본다
  const [mountedAt] = useState(() => Date.now() / 1000)
  const since = stationQuery.isFetchedAfterMount ? (stationQuery.data?.created_at ?? mountedAt) : null
  useEffect(() => {
    if (since === null) return
    const requests = requestsQuery.data?.requests.filter((item) => Date.parse(item.createdAt) / 1000 >= since)
    if (!requests) return
    // 처음 받은 목록과, 방송 시작 시각을 알게 돼 범위가 바뀐 직후 목록은 기준으로만 쓴다
    const previous = lastSince.current === since ? lastStatus.current : null
    lastSince.current = since
    lastStatus.current = new Map(requests.map((item) => [item.id, item.status]))
    if (!previous) return
    push(requests.filter((item) => previous.get(item.id) !== item.status).map(describeInputChange))
  }, [requestsQuery.data, since])

  const skipMutation = useMutation({
    mutationFn: (corner: string) => skipCorner(stationId, corner),
    onSuccess: ({ corner, discarded }) => push([{
      id: `skip-${Date.now()}`, at: Date.now() / 1000, tone: 'control',
      title: `코너 변경 요청 → ${cornerLabel(corner)}`,
      detail: `아직 내보내지 않은 멘트 ${discarded}개를 버렸어요. 이미 만든 소리가 나간 뒤 전환돼요 (약 15초)`,
    }]),
  })

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

        {/* 페르소나·스테이션 변경은 엔진 지원 전이라 버튼만 둔다 */}
        <div className="flex flex-wrap items-center gap-1.5">
          <h2 className="shrink-0 text-xs font-semibold text-text">페르소나</h2>
          {PERSONA_PRESETS.map((preset) => (
            <button
              key={preset.id}
              type="button"
              aria-pressed={preset.persona.djName === djName}
              disabled
              title="준비 중"
              className={chipClass(preset.persona.djName === djName)}
            >
              {preset.label}
            </button>
          ))}
        </div>

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
