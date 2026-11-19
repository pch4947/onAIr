import { BoardPostCollection } from '@/routes/mypage/BoardPostCollection'
import { NotificationCollection } from '@/routes/mypage/NotificationCollection'
import { ReplayCollection } from '@/routes/mypage/ReplayCollection'
import { getBookmarkedReplays, getLikedBoardPosts, getMyBoardPosts, getMyReplays } from '@/shared/api'
import type { MyPageTab } from '@/routes/mypage/mock'

export function MyPageTabContent({ tab }: { tab: MyPageTab }) {
  switch (tab) {
    case 'notifications':
      return <NotificationCollection />
    case 'myPosts':
      return (
        <BoardPostCollection
          queryKey="my-board-posts"
          fetcher={getMyBoardPosts}
          emptyText="아직 작성한 글이 없어요"
        />
      )
    case 'likedPosts':
      return (
        <BoardPostCollection
          queryKey="liked-board-posts"
          fetcher={getLikedBoardPosts}
          emptyText="아직 좋아요한 글이 없어요"
          removeOnUnlike
        />
      )
    case 'myBroadcastReplays':
      return (
        <ReplayCollection
          queryKey="my-station-replays"
          fetcher={getMyReplays}
          emptyText="아직 내가 진행한 방송 다시듣기가 없어요"
          deleteMode="destroy"
        />
      )
    case 'savedReplays':
      return (
        <ReplayCollection
          queryKey="bookmarked-replays"
          fetcher={getBookmarkedReplays}
          emptyText="아직 저장한 방송 다시듣기가 없어요"
          deleteMode="unbookmark"
        />
      )
  }
}
