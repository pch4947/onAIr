// FEATURE: 01/02_Listen_스트리밍 방송 화면 목업 데이터 (추후 WS/REST 응답으로 교체 예정)

export interface StationInfo {
  name: string
  isLive: boolean
  viewerCount: number
}

export interface Track {
  id: string
  cornerName: string
  title: string
  artist: string
  durationSec: number
  audioUrl: string
}

export type RequestStatusVariant = 'generating' | 'queued' | 'played' | 'rejected' | 'muted'

export interface RequestStatusItem {
  id: string
  text: string
  variant: RequestStatusVariant
  visibleToSelfOnly: boolean
}

export interface ChatMessage {
  id: string
  author: string
  text: string
}

export interface SurveyQuestion {
  id: string
  label: string
}

export const STATION_INFO: StationInfo = {
  name: 'AI 라디오 스테이션',
  isLive: true,
  viewerCount: 37,
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

export const SYNC_OFFSET_SEC = 0.3

export const MY_REQUEST_STATUSES: RequestStatusItem[] = [
  { id: '1', text: '사연 · GENERATING', variant: 'generating', visibleToSelfOnly: false },
  { id: '2', text: '신청곡 · QUEUED', variant: 'queued', visibleToSelfOnly: false },
  { id: '3', text: '사연 · PLAYED', variant: 'played', visibleToSelfOnly: false },
  { id: '4', text: 'REJECTED', variant: 'rejected', visibleToSelfOnly: true },
  { id: '5', text: '접수됨 · 확인 생략', variant: 'muted', visibleToSelfOnly: true },
]

export const CHAT_MESSAGES: ChatMessage[] = [
  { id: '1', author: '청취자1', text: '오늘 노래 너무 좋아요' },
  { id: '2', author: '청취자2', text: '신청곡 부탁드려요!' },
  { id: '3', author: '청취자3', text: '화이팅!!' },
  { id: '4', author: 'DJ', text: '신청 감사합니다' },
  { id: '5', author: '청취자4', text: '목소리 좋네요 ㅎㅎ' },
  { id: '6', author: '청취자5', text: '다음 곡 기대돼요' },
  { id: '7', author: 'DJ', text: '잠시 후 이어집니다' },
  { id: '8', author: '청취자6', text: '굿밤 되세요~' },
]

export const SURVEY_QUESTIONS: SurveyQuestion[] = [
  { id: 'responsiveness', label: '반응성 — AI가 요청에 얼마나 빨리 반응했나요?' },
  { id: 'waiting', label: '대기감 — 응답을 기다리는 동안 답답함을 느꼈나요?' },
  { id: 'naturalness', label: '자연스러움 — 방송 흐름이 자연스러웠나요?' },
  { id: 'disruption', label: '방해감 — 다른 청취자의 요청 처리로 방해받았다고 느꼈나요?' },
  { id: 'satisfaction', label: '만족도 — 전반적으로 만족스러웠나요?' },
]
