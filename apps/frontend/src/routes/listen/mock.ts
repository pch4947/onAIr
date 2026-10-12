// FEATURE: 01/02_Listen_스트리밍 방송 화면 목업 데이터 (백엔드 미연동 항목만 유지)

export interface StationInfo {
  name: string
  isLive: boolean
}

export interface Track {
  id: string
  cornerName: string
  title: string
  artist: string
  durationSec: number
  audioUrl: string
}

export interface SurveyQuestion {
  id: string
  label: string
}

export const STATION_INFO: StationInfo = {
  name: 'AI 라디오 스테이션',
  isLive: true,
}

export const PLAYLIST: Track[] = [
  {
    id: '1',
    cornerName: '감성 발라드 나이트',
    title: '새벽의 편지',
    artist: 'AI DJ 세라',
    durationSec: 12,
    audioUrl: '/mock-audio/track-1.wav',
  },
  {
    id: '2',
    cornerName: '오늘의 주제',
    title: '오늘 하루 이야기',
    artist: 'AI DJ 세라',
    durationSec: 10,
    audioUrl: '/mock-audio/track-2.wav',
  },
  {
    id: '3',
    cornerName: '심야 사연',
    title: '잔잔한 위로',
    artist: 'AI DJ 세라',
    durationSec: 14,
    audioUrl: '/mock-audio/track-3.wav',
  },
]

export const SURVEY_QUESTIONS: SurveyQuestion[] = [
  { id: 'responsiveness', label: '반응성 — AI가 요청에 얼마나 빨리 반응했나요?' },
  { id: 'waiting', label: '대기감 — 응답을 기다리는 동안 답답함을 느꼈나요?' },
  { id: 'naturalness', label: '자연스러움 — 방송 흐름이 자연스러웠나요?' },
  { id: 'disruption', label: '방해감 — 다른 청취자의 요청 처리로 방해받았다고 느꼈나요?' },
  { id: 'satisfaction', label: '만족도 — 전반적으로 만족스러웠나요?' },
]
