// 요청 상태 → 배지 색·짧은 문구·요청자 안내. v1(requested→…→played)과 계약 v2 값(10.4절)을 함께 받는다.
// 안내 문장은 docs/CORNER_DESIGN.md 9.1절 — 요청자 본인에게만 보인다

import type { RequestRecord } from '@/shared/types'

export type StatusVariant = 'generating' | 'queued' | 'played' | 'rejected' | 'expired'

export const STATUS_VARIANT_CLASS: Record<StatusVariant, string> = {
  generating: 'bg-status-generating',
  queued: 'bg-status-queued',
  played: 'bg-status-played',
  rejected: 'bg-status-rejected',
  expired: 'bg-status-muted',
}

const VARIANT: Record<string, StatusVariant> = {
  // v1
  requested: 'queued', screened: 'queued', queued: 'queued',
  generating: 'generating', generated: 'generating', mixing: 'generating',
  published: 'played', played: 'played',
  rejected: 'rejected', failed: 'rejected', expired: 'expired',
  // v2 (계약 10.4절)
  pooled: 'queued', inboxed: 'queued', held: 'queued',
  selected: 'generating',
  reflected: 'played', collected: 'played',
  not_on_air: 'expired', cancelled: 'expired',
}

const LABEL: Record<string, string> = {
  requested: '접수', screened: '접수', queued: '대기', pooled: '대기', inboxed: '대기', held: '대기',
  generating: '준비 중', generated: '준비 중', mixing: '준비 중', selected: '준비 중',
  published: '방송 중', played: '방송됨', reflected: '방송됨', collected: '반영됨',
  rejected: '거절', failed: '실패', expired: '만료', not_on_air: '미반영', cancelled: '취소',
}

type Kind = RequestRecord['kind']

// 신청곡은 멘트가 공개(published)돼도 곡은 아직 대기열에 있다
export function statusVariant(kind: Kind, status: string): StatusVariant {
  if (kind === 'song_request' && status === 'published') return 'queued'
  return VARIANT[status] ?? 'queued'
}

export function statusLabel(kind: Kind, status: string): string {
  if (kind === 'song_request' && status === 'published') return '대기'
  return LABEL[status] ?? status
}

// 요청자 본인에게 보이는 안내 (9.1절). 없으면 null
export function statusNote(kind: Kind, status: string): string | null {
  switch (status) {
    case 'pooled': return 'DJ가 채팅을 보고 있어요'
    case 'inboxed': return kind === 'song_request' ? '곡 소개 때 소개될 수 있어요' : '다음 문자 소개 시간 후보예요'
    case 'held': return '곡이 끝나면 DJ가 볼 수 있어요'
    case 'selected': return 'DJ가 멘트에 넣고 있어요'
    case 'reflected': return '방송에 나왔어요'
    case 'collected': return '투표에 반영됐어요'
    case 'not_on_air': return '이 방송은 청취자 의견을 방송에 반영하지 않아요'
    case 'expired': return '이번엔 소개되지 못했어요'
    case 'rejected': return '방송에 내보낼 수 없는 내용이에요'
    case 'cancelled': return '취소됐어요'
    default: return null
  }
}
