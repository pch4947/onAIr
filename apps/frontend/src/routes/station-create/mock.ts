// FEATURE: 00b_Main_방송 시작하기 목업 (추후 백엔드 중계 API로 교체 예정 — 이슈 #66)
// 고정 장르 목록은 packages/onair_schema의 Genre 타입과 맞춰야 한다 (팀 결정 필요, ENGINE_REDIS_CONTRACT.md 9.3절)

import type { Energy, Formality, Humor, Persona, PersonaFieldError, PersonaForm, PersonaStyle, Voice } from '@/shared/types'

export const GENRE_OPTIONS = [
  '발라드',
  '팝',
  '댄스/EDM',
  '힙합/랩',
  'R&B/소울',
  '록/밴드',
  '어쿠스틱/포크',
  '인디',
  '재즈',
  '트로트',
] as const

export const MAX_MUSIC_TASTE = 3

export const VOICE_OPTIONS: Voice[] = [
  { id: 'voice_dawn', name: '새벽', gender: 'female', description: '차분하고 낮은 톤' },
  { id: 'voice_breeze', name: '산들', gender: 'male', description: '부드럽고 중저음' },
  { id: 'voice_spark', name: '반짝', gender: 'female', description: '밝고 경쾌한 톤' },
  { id: 'voice_haze', name: '안개', gender: 'male', description: '느리고 낮게 깔리는 톤' },
]

const FORMALITY_PHRASE: Record<Formality, string> = {
  polite: '차분한 존댓말',
  casual: '편안한 반말',
}
const ENERGY_PHRASE: Record<Energy, string> = {
  low: '낮은 텐션으로',
  mid: '적당한 텐션으로',
  high: '높은 텐션으로',
}
const HUMOR_PHRASE: Record<Humor, string> = {
  rare: '농담은 드물게',
  some: '가끔 농담을 섞어',
  often: '자주 농담을 섞어',
}

function baseTone(style: PersonaStyle): string {
  return `${FORMALITY_PHRASE[style.formality]}, ${ENERGY_PHRASE[style.energy]} ${HUMOR_PHRASE[style.humor]} 진행한다`
}

const DRAFT_NAME_POOL = ['새벽', '한낮', '노을', '은하', '잔물결', '봄밤']
const DRAFT_CONCEPT_POOL = ['심야 스터디 라디오', '출근길 에너지 충전소', '주말 오후 산책 방송', '감성 발라드 전용 채널']

const POLITE_EXAMPLES = [
  '오늘도 와주셔서 고마워요',
  '지금 이 시간, 저랑 같이 보내요',
  '신청곡 들어오면 바로 틀어드릴게요',
  '잠깐 숨 고르고 다음 곡 들려드릴게요',
  '오늘 방송도 끝까지 함께해요',
]
const CASUAL_EXAMPLES = [
  '오늘도 와줘서 고마워',
  '지금 이 시간, 나랑 같이 보내자',
  '신청곡 오면 바로 틀어줄게',
  '잠깐 쉬었다가 다음 얘기 이어갈게',
  '오늘 방송도 끝까지 같이 가자',
]

function randomPick<T>(pool: readonly T[], exclude: T[] = []): T {
  const candidates = pool.filter((item) => !exclude.includes(item))
  const list = candidates.length > 0 ? candidates : pool
  return list[Math.floor(Math.random() * list.length)]
}

function delay(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export async function getVoices(): Promise<Voice[]> {
  await delay(300)
  return VOICE_OPTIONS
}

export async function generatePersonaDrafts(form: PersonaForm, count = 3): Promise<Persona[]> {
  await delay(1500)
  const usedNames: string[] = []
  return Array.from({ length: count }, () => {
    const djName = form.djName?.trim() || randomPick(DRAFT_NAME_POOL, usedNames)
    usedNames.push(djName)
    const forbidden = ['정치적 발언하기', '특정인 비하하기']
    if (form.style.energy === 'low') forbidden.push('큰 소리로 흥분하기')
    return {
      style: form.style,
      musicTaste: form.musicTaste,
      voice: form.voice,
      djName,
      concept: randomPick(DRAFT_CONCEPT_POOL),
      tone: baseTone(form.style),
      examples: form.style.formality === 'polite' ? POLITE_EXAMPLES : CASUAL_EXAMPLES,
      signaturePhrases: ['천천히 가요'],
      forbidden,
    }
  })
}

export async function checkPersonaDraft(
  persona: Persona,
): Promise<{ ok: true } | { ok: false; errors: PersonaFieldError[] }> {
  await delay(400)
  const errors: PersonaFieldError[] = []

  if (persona.djName.trim().length < 1 || persona.djName.length > 20) {
    errors.push({ field: 'persona.dj_name', reason: '1~20자로 입력해주세요' })
  }
  if (persona.concept.trim().length < 1 || persona.concept.length > 60) {
    errors.push({ field: 'persona.concept', reason: '1~60자로 입력해주세요' })
  }
  if (persona.tone.trim().length < 1 || persona.tone.length > 100) {
    errors.push({ field: 'persona.tone', reason: '1~100자로 입력해주세요' })
  }
  if (persona.examples.length < 3 || persona.examples.length > 5) {
    errors.push({ field: 'persona.examples', reason: '예시 멘트는 3~5개여야 해요' })
  }
  persona.examples.forEach((example, index) => {
    if (example.trim().length < 1 || example.length > 150) {
      errors.push({ field: `persona.examples.${index}`, reason: '1~150자로 입력해주세요' })
    }
  })
  persona.signaturePhrases.forEach((phrase, index) => {
    if (phrase.trim().length < 1 || phrase.length > 30) {
      errors.push({ field: `persona.signature_phrases.${index}`, reason: '1~30자로 입력해주세요' })
    }
  })
  persona.forbidden.forEach((item, index) => {
    if (item.trim().length < 1 || item.length > 50) {
      errors.push({ field: `persona.forbidden.${index}`, reason: '1~50자로 입력해주세요' })
    }
  })

  return errors.length > 0 ? { ok: false, errors } : { ok: true }
}

export async function createStationMock(input: {
  persona: Persona
  firstSongUrl?: string
  broadcastMinutes: number
  topic?: string
}): Promise<{ stationId: string }> {
  await delay(800)
  void input
  return { stationId: `st_${Math.random().toString(36).slice(2, 8)}` }
}
