export interface AuthFeature {
  icon: string
  text: string
}

export const AUTH_FEATURES: AuthFeature[] = [
  { icon: '🎙', text: '진행 부담 없이 AI DJ가 방송을 이어가요' },
  { icon: '💬', text: '사연과 요청이 실시간으로 방송에 반영돼요' },
  { icon: '👥', text: '같은 방송을 듣는 사람들과 취향을 나눠요' },
]

export type SocialProvider = 'naver' | 'kakao' | 'google'

export interface SocialProviderConfig {
  id: SocialProvider
  label: string
}

export const SOCIAL_PROVIDERS: SocialProviderConfig[] = [
  { id: 'naver', label: '네이버로 계속하기' },
  { id: 'kakao', label: '카카오로 계속하기' },
  { id: 'google', label: 'Google로 계속하기' },
]
