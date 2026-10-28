import { useQuery } from '@tanstack/react-query'
import { BoardPostCollection } from '@/routes/mypage/BoardPostCollection'
import { NotificationCollection } from '@/routes/mypage/NotificationCollection'
import { ReplayCollection } from '@/routes/mypage/ReplayCollection'
import { getBookmarkedReplays, getBroadcastState, getLikedBoardPosts, getMyBoardPosts, getMyStationReplays } from '@/shared/api'
import type { MyPageTab } from '@/routes/mypage/mock'

export function MyPageTabContent({ tab }: { tab: MyPageTab }) {
  const broadcastStateQuery = useQuery({ queryKey: ['broadcast-state'], queryFn: getBroadcastState })
  const stationId = broadcastStateQuery.data?.stationId

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
      if (stationId) {
        return (
          <ReplayCollection
            queryKey="my-station-replays"
            fetcher={(params) => getMyStationReplays(stationId, params)}
            emptyText="아직 내가 진행한 방송 다시듣기가 없어요"
            deleteMode="destroy"
          />
        )
      }
      return broadcastStateQuery.isError
        ? <p className="text-[13px] text-danger">방송 정보를 불러오지 못했어요</p>
        : <p className="text-[13px] text-text-muted">불러오는 중...</p>
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
