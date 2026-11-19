// 방송 만들기의 첫 노래 값 — 링크를 직접 적거나 검색 결과에서 고른다 (#346)

import { normalizeYoutubeLink } from '@/routes/listen/trackClient'
import type { TrackCandidate } from '@/routes/listen/trackClient'

export interface FirstSongValue {
  text: string
  selected: TrackCandidate | null
}

export const EMPTY_FIRST_SONG: FirstSongValue = { text: '', selected: null }

export const LOOKS_LIKE_LINK = /^(https?:\/\/|www\.)/i

/** 방송 생성에 보낼 주소. 링크를 직접 적었으면 그 링크, 아니면 검색 결과에서 고른 곡. */
export function firstSongUrl(value: FirstSongValue): string | null {
  return normalizeYoutubeLink(value.text) ?? value.selected?.url ?? null
}

/** 적어 둔 내용이 있는데 보낼 곡이 정해지지 않았으면 그 이유. */
export function firstSongError(value: FirstSongValue): string | null {
  const text = value.text.trim()
  if (!text || firstSongUrl(value)) return null
  return LOOKS_LIKE_LINK.test(text) ? '올바른 유튜브 영상 링크를 입력해주세요' : '검색한 뒤 첫 노래로 쓸 영상을 골라주세요'
}
