import type { Energy, Formality, Humor, PersonaStyle } from '@/shared/types'

// DJ 말투 선택지의 화면 이름 — 방송 만들기 폼과 청취 화면의 DJ 정보 창(#353)이 같이 쓴다
export const FORMALITY_OPTIONS: { value: Formality; label: string }[] = [
  { value: 'polite', label: '존댓말' },
  { value: 'casual', label: '반말' },
]
export const ENERGY_OPTIONS: { value: Energy; label: string }[] = [
  { value: 'low', label: '차분하게' },
  { value: 'mid', label: '보통' },
  { value: 'high', label: '활기차게' },
]
export const HUMOR_OPTIONS: { value: Humor; label: string }[] = [
  { value: 'rare', label: '드물게' },
  { value: 'some', label: '가끔' },
  { value: 'often', label: '자주' },
]

function labelOf<T extends string>(options: { value: T; label: string }[], value: T | null | undefined) {
  return options.find((option) => option.value === value)?.label ?? null
}

// 말투 선택을 한 줄 꼬리표로 — 값이 빠졌거나 모르는 값이면 그 꼬리표만 뺀다
export function styleTags(style: Partial<PersonaStyle> | null | undefined): string[] {
  const humor = labelOf(HUMOR_OPTIONS, style?.humor)
  return [
    labelOf(FORMALITY_OPTIONS, style?.formality),
    labelOf(ENERGY_OPTIONS, style?.energy),
    humor && `유머 ${humor}`,
  ].filter((tag): tag is string => Boolean(tag))
}
