export interface AuthFeature {
  icon: string
  text: string
}

export const AUTH_FEATURES: AuthFeature[] = [
  { icon: '🎙', text: '24시간 끊김 없는 AI DJ 방송' },
  { icon: '💬', text: '내 사연과 신청곡이 실시간으로 반영' },
  { icon: '🔖', text: '놓친 방송은 다시듣기로 언제든지' },
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
