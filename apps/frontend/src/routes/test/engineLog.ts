// 엔진 로그 문구 — 값의 뜻은 docs/CORNER_DESIGN.md, docs/DEMO_SCENARIO.md 3절, docs/ENGINE_REDIS_CONTRACT.md 6.1·10.4절
import { cornerLabel } from '@/routes/test/corners'
import type { EngineEvent } from '@/shared/api'
import type { CornerInputMode, CurrentCorner, RequestRecord } from '@/shared/types'

export interface LogEntry {
  id: string
  at: number // 초
  // decision 선별·생성 / reflect 화제 전환·회상·집계 / policy 코너·방송 / input 입력 / control 실패·대체·요청 / muted 소재
  tone: 'decision' | 'reflect' | 'policy' | 'input' | 'control' | 'muted'
  title: string
  detail?: string
}

const SITUATION_LABEL: Record<string, string> = { quiet: '조용함', normal: '보통', flood: '채팅 폭주' }
const CHAT_TYPE_LABEL: Record<string, string> = { reaction: '반응형', standalone: '독립형' }
// 엔진 inputs/classify.py — 위에서부터 처음 맞는 규칙으로 유형을 정한다
const RULE_LABEL: Record<string, string> = {
  pointing: '지시어', anchor_keyword: '지금 나오는 것과 키워드 겹침', short_reaction: '짧은 반응', default: '해당 규칙 없음',
}

// 엔진 입력 종류(계약 10.3절)와 백엔드 요청 종류(story = 채팅창 입력)
const INPUT_KIND_LABEL: Record<string, string> = {
  chat: '채팅', story: '채팅', message: '사연', song_request: '신청곡', board_story: '게시판 사연', vote: '투표',
}

const MODE_LABEL: Record<CornerInputMode, string> = {
  quote: '골라 인용', steer: '인용하고 화제로 삼음', read: '그대로 낭독', feature: '주 소재로 다룸',
  solicit: '질문하고 답을 모음', closed: '반영 안 함', defer: '다음 코너로 미룸', none: '해당 없음',
}

const STATUS_LABEL: Record<string, string> = {
  requested: '접수', screened: '안전 검사 통과', queued: '받은편지함·후보 풀에서 대기', generating: '멘트에 들어감 (생성 중)',
  generated: '멘트 생성됨', mixing: '송출 준비', published: '방송에 나감', played: '방송됨',
  rejected: '안전 검사 탈락', failed: '처리 실패', expired: '선택되지 못하고 만료',
  // 계약 v2 (10.4절)
  pooled: '채팅 후보 풀에 들어감', inboxed: '받은편지함에서 대기', collected: '투표로 수집됨', held: '곡 끝날 때까지 보류',
  not_on_air: '방송 미반영 (이 형식)', selected: '멘트에 들어감 (생성 중)', reflected: '방송에 나감', cancelled: '취소됨',
}

function quote(text: string | null | undefined, max = 24) {
  if (!text) return '(내용 없음)'
  const flat = text.replace(/\s+/g, ' ').trim()
  return `“${flat.length > max ? `${flat.slice(0, max)}…` : flat}”`
}

const EVENT_TONE: Record<string, LogEntry['tone']> = {
  generation: 'decision', selection: 'decision',
  steer: 'reflect', callback: 'reflect', aggregate: 'reflect',
  corner: 'policy', station: 'policy', topic_plan: 'policy',
  input: 'input', aired: 'input',
  track: 'muted', rss: 'muted', story_judge: 'muted',
}
// 생성이 실패하거나 대체·차단·인용 누락이면 눈에 띄게
const TROUBLE = /대체 멘트|실패|차단|누락|LLM 거부|건너뜀|코너 대체/

function text(value: unknown) {
  return typeof value === 'string' ? value : null
}

// 엔진 문구(summary)는 그대로 두고, 원자료로 근거 한 줄을 덧붙인다
function eventDetail(event: EngineEvent): string | undefined {
  const data = event.data ?? {}
  if (event.kind === 'input') {
    const chatType = text(data.chat_type)
    const rule = text(data.rule)
    const type = chatType ? ` · ${CHAT_TYPE_LABEL[chatType] ?? chatType}${rule ? `(${RULE_LABEL[rule] ?? rule})` : ''}` : ''
    return `${quote(text(data.body), 40)}${type}`
  }
  if (event.kind === 'selection') {
    const situation = text(data.situation)
    return situation ? `채팅 상황 ${SITUATION_LABEL[situation] ?? situation}` : undefined
  }
  return undefined
}

export function describeEvent(event: EngineEvent): LogEntry {
  const tone = TROUBLE.test(event.summary) ? 'control' : EVENT_TONE[event.kind] ?? 'muted'
  return { id: `e-${event.id}`, at: event.at, tone, title: event.summary, detail: eventDetail(event) }
}

export function describePolicy(corner: CurrentCorner): LogEntry {
  const rules = Object.entries(corner.contract)
    .filter(([, mode]) => mode && mode !== 'none')
    .map(([kind, mode]) => `${INPUT_KIND_LABEL[kind] ?? kind}: ${MODE_LABEL[mode as CornerInputMode]}`)
  return {
    id: `p-${corner.corner}-${Date.now()}`,
    at: Date.now() / 1000,
    tone: 'policy',
    title: `방송 중인 코너 → ${corner.title || cornerLabel(corner.corner)}`,
    detail: rules.length ? `입력 대응 정책 — ${rules.join(' / ')}` : undefined,
  }
}

export function describeInputChange(request: RequestRecord): LogEntry {
  const kind = INPUT_KIND_LABEL[request.kind] ?? '입력'
  const body = request.kind === 'song_request' && request.trackTitle ? request.trackTitle : request.prompt
  const at = Date.now() / 1000
  return {
    // 생성이 실패하면 상태가 대기로 되돌아갈 수 있어 같은 상태가 두 번 찍힐 수 있다
    id: `r-${request.id}-${request.status}-${at}`,
    at,
    tone: ['rejected', 'expired', 'not_on_air', 'cancelled'].includes(request.status) ? 'muted' : 'input',
    title: `${kind} ${quote(body)} → ${STATUS_LABEL[request.status] ?? request.status}`,
  }
}
