// 테스트 화면 코너 버튼 — 코너 키는 apps/engine/config/formats/*.yaml과 같다
import type { StationFormat, TalkCorner } from '@/shared/types'

export const CORNER_LABELS: Record<string, string> = {
  opening: '오프닝',
  closing: '엔딩',
  topic_talk: '주제 토크',
  text_corner: '문자 소개',
  story_corner: '게시판 사연',
  dj_vs_listeners: 'DJ 대 청취자',
  music_intro: '곡 소개',
  artist_talk: '아티스트 이야기',
  headline: '헤드라인',
  section: '분야별 브리핑',
  recap: '정리',
  filler: '연결 멘트',
  bridge: '브릿지',
  music: '음악',
}

// 엔진이 호스트가 코너를 고르지 않았을 때 쓰는 기본 본 코너 (talk.yaml default_main)
const DEFAULT_TALK_CORNERS: TalkCorner[] = ['story_corner', 'topic_talk']

// 형식 순환에 들어 있어 건너뛸 수 있는 코너. 토크는 고른 본 코너 + 늘 도는 문자 소개·곡 소개
export function skippableCorners(format: StationFormat, corners?: TalkCorner[] | null): string[] {
  if (format === 'music') return ['artist_talk', 'music_intro']
  if (format === 'news') return ['headline', 'section', 'recap']
  const main = corners?.length ? corners : DEFAULT_TALK_CORNERS
  return [...main.filter((corner) => corner !== 'text_corner'), 'text_corner', 'music_intro']
}

export function cornerLabel(key: string): string {
  return CORNER_LABELS[key] ?? key
}
