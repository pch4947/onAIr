// 백엔드 요청 상태 머신(requested→...→played, rejected/failed)을 UI 배지로 매핑

export type StatusVariant = 'generating' | 'queued' | 'played' | 'rejected'

export const STATUS_VARIANT_CLASS: Record<StatusVariant, string> = {
  generating: 'bg-status-generating',
  queued: 'bg-status-queued',
  played: 'bg-status-played',
  rejected: 'bg-status-rejected',
}

export const STATUS_VARIANT: Record<string, StatusVariant> = {
  requested: 'queued',
  screened: 'queued',
  queued: 'queued',
  generating: 'generating',
  generated: 'generating',
  mixing: 'generating',
  published: 'played',
  played: 'played',
  rejected: 'rejected',
  failed: 'rejected',
}

export const KIND_LABEL: Record<'story' | 'mood', string> = {
  story: '사연',
  mood: '신청곡',
}

const PENDING_STATUSES = new Set(['requested', 'screened', 'queued', 'generating', 'generated', 'mixing'])

export function isPendingStatus(status: string): boolean {
  return PENDING_STATUSES.has(status)
}
