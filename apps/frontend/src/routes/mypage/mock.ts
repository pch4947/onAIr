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
    subtitle: '내가 쓴 글에 댓글이 달리면 알려드려요',
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

