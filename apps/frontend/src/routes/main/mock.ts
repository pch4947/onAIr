// FEATURE: 00_Main_방송 선택 목업 데이터 (추후 백엔드 API 응답으로 교체 예정)

export interface Station {
  id: string
  title: string
  hashtags: string[]
  viewerCount: number
}

export const STATIONS: Station[] = [
  {
    id: '1',
    title: '감성 발라드 나이트 스테이션',
    hashtags: ['#감성발라드', '#새벽감성', '#잔잔한'],
    viewerCount: 37,
  },
  {
    id: '2',
    title: '오늘의 주제 스테이션',
    hashtags: ['#토크', '#힐링', '#차분한'],
    viewerCount: 15,
  },
  {
    id: '3',
    title: '심야 사연 스테이션',
    hashtags: ['#심야라디오', '#사연', '#포근한'],
    viewerCount: 22,
  },
  {
    id: '4',
    title: '감성 발라드 나이트 스테이션',
    hashtags: ['#감성발라드', '#새벽감성', '#잔잔한'],
    viewerCount: 37,
  },
  {
    id: '5',
    title: '오늘의 주제 스테이션',
    hashtags: ['#토크', '#힐링', '#차분한'],
    viewerCount: 15,
  },
  {
    id: '6',
    title: '심야 사연 스테이션',
    hashtags: ['#심야라디오', '#사연', '#포근한'],
    viewerCount: 22,
  },
]
