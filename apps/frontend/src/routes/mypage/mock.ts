// FEATURE: 05_MyPage_개인 페이지 목업 데이터 (추후 REST 응답으로 교체 예정)

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
}

export const MYPAGE_TABS: MyPageTabConfig[] = [
  { id: 'notifications', label: '알림' },
  { id: 'myPosts', label: '내 게시판 글 모아보기' },
  { id: 'myBroadcastReplays', label: '내 방송 다시보기' },
  { id: 'savedReplays', label: '저장한 방송 다시보기' },
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
