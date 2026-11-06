// 엔진 로그 문구 — 값의 뜻은 docs/CORNER_DESIGN.md, docs/ENGINE_REDIS_CONTRACT.md 6.1·10.4절
import { cornerLabel } from '@/routes/test/corners'
import type { DecisionInput, EngineDecision } from '@/shared/api'
import type { CornerInputMode, CurrentCorner, RequestRecord } from '@/shared/types'

export interface LogEntry {
  id: string
  at: number // 초
  tone: 'decision' | 'policy' | 'input' | 'control' | 'muted'
  title: string
  detail?: string
}

const STEP_LABEL: Record<string, string> = {
  opening_greeting: '시작 인사', topic_intro: '주제 소개', corner_preview: '코너 예고', talk_open: '이야기 꺼내기',
  talk: '이야기 잇기', artist_talk: '아티스트 이야기', music_intro: '곡 소개', story: '사연 읽기',
  text_read: '문자 읽기', game_round: '투표 권하기', news_headline: '헤드라인', news_item: '기사 전달',
  news_recap: '소식 정리', closing_lookback: '돌아보기', closing_farewell: '끝인사', filler: '브릿지',
}

const SITUATION_LABEL: Record<string, string> = { quiet: '조용함', normal: '보통', flood: '채팅 폭주' }
const CRITERION_LABEL: Record<string, string> = { recency: '최근성 우선', relevance: '화제 관련성 우선', diversity: '다양한 사람 우선' }
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
}

function quote(text: string | null | undefined, max = 24) {
  if (!text) return '(내용 없음)'
  const flat = text.replace(/\s+/g, ' ').trim()
  return `“${flat.length > max ? `${flat.slice(0, max)}…` : flat}”`
}

function inputLine(input: DecisionInput) {
  const kind = INPUT_KIND_LABEL[input.kind ?? ''] ?? '입력'
  const rule = input.rule ? `(${RULE_LABEL[input.rule] ?? input.rule})` : ''
  const type = input.chat_type ? ` · ${CHAT_TYPE_LABEL[input.chat_type]}${rule}` : ''
  return `${kind}${type} ${quote(input.body)}`
}

export function describeDecision(decision: EngineDecision): LogEntry {
  const base = { id: `d-${decision.id}`, at: decision.at }
  const where = decision.corner ? cornerLabel(decision.corner) : '코너 밖'
  if (decision.action === 'glue') return { ...base, tone: 'muted', title: `연결음 · ${where}` }
  if (decision.action === 'no_material') return { ...base, tone: 'muted', title: `${where} · 낼 소재가 없어 기다림` }
  if (decision.action === 'tts_blocked') return { ...base, tone: 'muted', title: '음성 합성 한도 초과 · 잠시 멈춤' }
  if (decision.action === 'idle') return { ...base, tone: 'muted', title: '엔딩까지 편성을 마침' }

  const step = decision.step ? STEP_LABEL[decision.step] ?? decision.step : ''
  const lines: string[] = []
  if (decision.situation || decision.criterion) {
    lines.push(`채팅 상황 ${SITUATION_LABEL[decision.situation ?? ''] ?? '-'} · 선별 기준 ${CRITERION_LABEL[decision.criterion ?? ''] ?? '-'}`)
  }
  if (decision.quotes.length) {
    lines.push(...decision.quotes.map((input) => `✔ 인용 ${inputLine(input)}`))
  }
  const quoted = new Set(decision.quotes.map((input) => input.request_id))
  const skipped = decision.pending.filter((input) => !quoted.has(input.request_id))
  if (skipped.length) {
    lines.push(...skipped.slice(0, 3).map((input) => `· 보류 ${inputLine(input)}`))
    if (skipped.length > 3) lines.push(`· 그 밖에 ${skipped.length - 3}건 대기`)
  }
  return {
    ...base,
    tone: 'decision',
    title: `멘트 생성 · ${where}${step ? ` / ${step}` : ''}${decision.quotes.length ? ` · 입력 ${decision.quotes.length}건 반영` : ''}`,
    detail: lines.join('\n') || undefined,
  }
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
    tone: request.status === 'rejected' || request.status === 'expired' ? 'muted' : 'input',
    title: `${kind} ${quote(body)} → ${STATUS_LABEL[request.status] ?? request.status}`,
  }
}
