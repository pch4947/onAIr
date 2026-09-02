// FEATURE: 03_Admin_운영자 콘솔 목업 데이터 (추후 WS/REST 응답으로 교체 예정)

export interface AdminOverview {
  isLive: boolean
  liveLabel: string
  viewerCount: number
  pendingRequestCount: number
}

export interface BufferStatus {
  dTotalSec: number
  thresholdSec: number
  state: string
}

export interface PolicyOption {
  id: string
  label: string
}

export interface RequestLogItem {
  id: string
  time: string
  request: string
  status: string
  policyLabel: string
}

export const ADMIN_OVERVIEW: AdminOverview = {
  isLive: true,
  liveLabel: '정상 송출 중',
  viewerCount: 37,
  pendingRequestCount: 2,
}

export const BUFFER_STATUS: BufferStatus = {
  dTotalSec: 24,
  thresholdSec: 20,
  state: '정상',
}

export const POLICY_OPTIONS: PolicyOption[] = [
  { id: 'A', label: 'A. 경계 대기' },
  { id: 'B', label: 'B. 즉시 채배' },
  { id: 'C', label: 'C. 접수+지연' },
]

export const CURRENT_POLICY_ID = 'C'

export const MANUAL_APPROVAL_ENABLED = false

export const REQUEST_LOG: RequestLogItem[] = [
  {
    id: '1',
    time: '21:04:12',
    request: '사연: 오늘 하루 힘들었어요',
    status: 'GENERATING',
    policyLabel: '정책 C',
  },
  {
    id: '2',
    time: '21:03:58',
    request: '신청곡: OOO - 좋은 날',
    status: 'QUEUED',
    policyLabel: '정책 C',
  },
  {
    id: '3',
    time: '21:02:30',
    request: '사연: 응원 부탁드려요',
    status: 'PLAYED',
    policyLabel: '정책 C',
  },
  {
    id: '4',
    time: '21:01:47',
    request: '사연: 정치 얘기 좀 해주세요',
    status: 'REJECTED (L1)',
    policyLabel: '—',
  },
  {
    id: '5',
    time: '21:01:02',
    request: '신청곡: 무드 - 잔잔한 밤',
    status: '접수 · 확인 생략',
    policyLabel: '정책 C',
  },
]
