export interface UserProfile {
  name: string
  avatarInitial: string
}

export const CURRENT_USER: UserProfile = {
  name: '사용자',
  avatarInitial: '사',
}

export type MyPageTab = 'notifications' | 'myPosts' | 'myBroadcastReplays' | 'savedReplays'

export interface MyPageTabConfig {
  id: MyPageTab
  label: string
  subtitle: string
}

export const MYPAGE_TABS: MyPageTabConfig[] = [
  {
    id: 'notifications',
    label: '알림',
    subtitle: '내가 쓴 사연이 방송에 소개되는 순간을 실시간으로 알려드려요',
  },
  {
    id: 'myPosts',
    label: '내 게시판 글 모아보기',
    subtitle: '내가 남긴 글과 방송 기록을 모아볼 수 있어요',
  },
  {
    id: 'myBroadcastReplays',
    label: '내 방송 다시보기',
    subtitle: '내가 진행했던 방송을 다시 들어볼 수 있어요',
  },
  {
    id: 'savedReplays',
    label: '저장한 방송 다시보기',
    subtitle: '저장해 둔 다른 사람의 방송을 다시 들어볼 수 있어요',
  },
]

export interface NotificationItem {
  id: string
  title: string
  description: string
  isUnread: boolean
}

export const NOTIFICATIONS: NotificationItem[] = [
  {
    id: '1',
    title: '곧 방송에 소개돼요',
    description:
      '"오늘 하루 위로가 필요해요" 사연이 <감성 발라드 나이트>에서 곧 소개돼요 · 시작까지 10분',
    isUnread: true,
  },
  {
    id: '2',
    title: '방송에 소개됐어요',
    description: '"친구와 화해하고 싶어요" 사연이 <심야 사연 라디오>에서 방금 소개됐어요 · 다시듣기 가능',
    isUnread: true,
  },
  {
    id: '3',
    title: '새 댓글이 달렸어요',
    description: '라디오러버 님이 내 사연에 댓글을 남겼어요 · 1시간 전',
    isUnread: false,
  },
  {
    id: '4',
    title: '저장한 방송 다시보기 준비 완료',
    description: '<오늘의 주제> 방송 다시듣기를 이용할 수 있어요 · 어제',
    isUnread: false,
  },
]

export interface MyBoardPost {
  id: string
  title: string
  boardName: string
  timeAgo: string
  commentCount: number
}

export const MY_BOARD_POSTS: MyBoardPost[] = [
  {
    id: '1',
    title: '오늘 하루 위로가 필요해요',
    boardName: '감성 발라드 나이트 게시판',
    timeAgo: '3일 전',
    commentCount: 3,
  },
  {
    id: '2',
    title: '친구와 화해하고 싶어요',
    boardName: '심야 사연 게시판',
    timeAgo: '5일 전',
    commentCount: 3,
  },
  {
    id: '3',
    title: '이 노래 신청해도 될까요?',
    boardName: '오늘의 주제 게시판',
    timeAgo: '1주 전',
    commentCount: 1,
  },
]

export interface ReplayItem {
  id: string
  title: string
  meta: string
}

export const MY_BROADCAST_REPLAYS: ReplayItem[] = [
  { id: '1', title: '감성 발라드 나이트', meta: '2026.08.30 방송 · 청취자 132명 · 1시간 32분' },
  { id: '2', title: '심야 사연 라디오', meta: '2026.08.25 방송 · 청취자 98명 · 2시간 05분' },
  { id: '3', title: '오늘의 주제: 첫사랑', meta: '2026.08.18 방송 · 청취자 210명 · 1시간 48분' },
]

export const SAVED_REPLAYS: ReplayItem[] = [
  { id: '1', title: '새벽 감성 플레이리스트', meta: '라디오러버 · 저장함 · 2026.08.28' },
  { id: '2', title: '오늘의 주제: 여행', meta: '별밤지기 · 저장함 · 2026.08.20' },
  { id: '3', title: '잔잔한 밤 사연', meta: '조용한밤 · 저장함 · 2026.08.15' },
]
