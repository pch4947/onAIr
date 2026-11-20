import type { Voice } from '@/shared/types'

// 방송 만들기 폼에 보여줄 추천 목소리 (#310).
// 엔진 목록(한국어 117개)은 이름·설명이 영어라 고르기 어려워, 방송에 어울리는 것만 한국어 이름으로 보여준다.
// 화면 표시만 줄이는 것이고 엔진은 전체 목록으로 검증한다. 영어 설명 기준 임시안이라 청취 후 바꾸면 이 목록만 고친다.
const CURATED: { id: string; label: string; gender: '여성' | '남성'; age: string }[] = [
  { id: 'ko-kr-storyteller-9', label: '차분하고 따뜻한 목소리', gender: '여성', age: '40대' },
  { id: 'ko-kr-assistant-3', label: '밝고 산뜻한 목소리', gender: '여성', age: '20대' },
  { id: 'ko-kr-podcaster-4', label: '활기차고 재미있는 목소리', gender: '여성', age: '40대' },
  { id: 'ko-kr-assistant-9', label: '느긋하고 편안한 목소리', gender: '여성', age: '40대' },
  { id: 'ko-kr-storyteller-6', label: '부드럽고 울림 있는 목소리', gender: '여성', age: '20대' },
  { id: 'ko-kr-podcaster-9', label: '다정한 라디오 DJ 목소리', gender: '남성', age: '40대' },
  { id: 'ko-kr-tutor-1', label: '차분하고 깊은 목소리', gender: '남성', age: '50대' },
  { id: 'ko-kr-tutor-6', label: '에너지 넘치는 목소리', gender: '남성', age: '20대' },
  { id: 'ko-kr-commercial-5', label: '토크쇼처럼 친근한 목소리', gender: '남성', age: '20대' },
  { id: 'ko-kr-podcaster-2', label: '경쾌한 부산 사투리', gender: '남성', age: '20대' },
]

export interface VoiceChoiceGroup {
  label: string | null
  choices: { id: string; label: string }[]
}

// 드롭다운에 넣을 선택지. 추천 목록 중 엔진에 있는 것만 성별로 묶는다.
// 추천이 하나도 없으면(로컬 dummy 엔진 등) 엔진 목록을 그대로 보여준다.
export function voiceChoiceGroups(voices: Voice[]): VoiceChoiceGroup[] {
  const available = new Set(voices.map((voice) => voice.id))
  const curated = CURATED.filter((item) => available.has(item.id))
  if (curated.length === 0) {
    return [{ label: null, choices: voices.map((voice) => ({ id: voice.id, label: voice.name })) }]
  }
  return (['여성', '남성'] as const)
    .map((gender) => ({
      label: gender,
      choices: curated.filter((item) => item.gender === gender).map((item) => ({ id: item.id, label: `${item.label} · ${item.age}` })),
    }))
    .filter((group) => group.choices.length > 0)
}

// 청취 화면의 DJ 정보 창(#353)에 보여 줄 목소리 이름 — 추천 목록에 없는 목소리면 null (영어 이름은 보이지 않는다)
export function curatedVoiceLabel(id: string | null | undefined): string | null {
  const item = CURATED.find((voice) => voice.id === id)
  return item ? `${item.label} (${item.gender} ${item.age})` : null
}

// 폼을 처음 열 때 고를 목소리 — 추천 목록의 첫 번째 (없으면 엔진 목록의 첫 번째)
export function defaultVoiceId(voices: Voice[]): string {
  return voiceChoiceGroups(voices)[0]?.choices[0]?.id ?? ''
}
