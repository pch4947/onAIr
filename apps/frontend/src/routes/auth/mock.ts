import type { ComponentType } from 'react'
import { ChatBubbleIcon, MicIcon, UsersIcon } from '@/app/icons'

export interface AuthFeature {
  icon: ComponentType<{ className?: string }>
  text: string
}

export const AUTH_FEATURES: AuthFeature[] = [
  { icon: MicIcon, text: '진행 부담 없이 AI DJ가 방송을 이어가요' },
  { icon: ChatBubbleIcon, text: '사연과 요청이 실시간으로 방송에 반영돼요' },
  { icon: UsersIcon, text: '같은 방송을 듣는 사람들과 취향을 나눠요' },
]
