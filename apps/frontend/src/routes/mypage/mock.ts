export type MyPageTab = 'notifications' | 'myPosts' | 'likedPosts' | 'myBroadcastReplays' | 'savedReplays'

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
    label: '내가 쓴 글',
    subtitle: '내가 게시판에 남긴 글을 모아볼 수 있어요',
  },
  {
    id: 'likedPosts',
    label: '좋아요한 글',
    subtitle: '좋아요를 누른 게시판 글을 모아볼 수 있어요',
  },
  {
    id: 'myBroadcastReplays',
    label: '내 방송 다시듣기',
    subtitle: '내가 진행했던 방송을 다시 들어볼 수 있어요',
  },
  {
    id: 'savedReplays',
    label: '저장한 방송 다시듣기',
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
    title: '저장한 방송 다시듣기 준비 완료',
    description: '<오늘의 주제> 방송 다시듣기를 이용할 수 있어요 · 어제',
    isUnread: false,
  },
]

