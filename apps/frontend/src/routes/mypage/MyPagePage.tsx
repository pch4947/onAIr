// 05_MyPage_개인 페이지

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Sidebar } from '@/app/Sidebar'
import { BoardPostCollection } from '@/routes/mypage/BoardPostCollection'
import { NotificationCollection } from '@/routes/mypage/NotificationCollection'
import { ReplayCollection } from '@/routes/mypage/ReplayCollection'
import { clearAccessToken } from '@/shared/authToken'
import { getBookmarkedReplays, getBroadcastState, getLikedBoardPosts, getMyBoardPosts, getMyStationReplays } from '@/shared/api'
import { MYPAGE_TABS } from '@/routes/mypage/mock'
import type { MyPageTab } from '@/routes/mypage/mock'

export function MyPagePage() {
  const navigate = useNavigate()
  const [activeTab, setActiveTab] = useState<MyPageTab>('notifications')
  const activeTabConfig = MYPAGE_TABS.find((tab) => tab.id === activeTab) ?? MYPAGE_TABS[0]
  const broadcastStateQuery = useQuery({ queryKey: ['broadcast-state'], queryFn: getBroadcastState })
  const stationId = broadcastStateQuery.data?.stationId

  return (
    <div className="flex min-h-screen">
      <Sidebar />

      <main className="flex-1 bg-surface p-8">
        <header className="mb-6 flex items-start justify-between">
          <div className="flex flex-col gap-1">
            <h1 className="text-2xl font-semibold text-[#1a1a1f]">개인 페이지</h1>
            <p className="text-[13px] text-text-muted">{activeTabConfig.subtitle}</p>
          </div>
          <button
            type="button"
            onClick={() => {
              clearAccessToken()
              navigate('/login')
            }}
            className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted"
          >
            로그아웃
          </button>
        </header>

        <nav className="mb-6 flex gap-7 text-sm">
          {MYPAGE_TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              className={
                activeTab === tab.id ? 'font-semibold text-primary' : 'font-medium text-text-muted'
              }
            >
              {tab.label}
            </button>
          ))}
        </nav>

        {activeTab === 'notifications' && <NotificationCollection />}

        {activeTab === 'myPosts' && (
          <BoardPostCollection
            queryKey="my-board-posts"
            fetcher={getMyBoardPosts}
            emptyText="아직 작성한 글이 없어요"
          />
        )}

        {activeTab === 'likedPosts' && (
          <BoardPostCollection
            queryKey="liked-board-posts"
            fetcher={getLikedBoardPosts}
            emptyText="아직 좋아요한 글이 없어요"
            removeOnUnlike
          />
        )}

        {activeTab === 'myBroadcastReplays' && (
          stationId ? (
            <ReplayCollection
              queryKey="my-station-replays"
              fetcher={(params) => getMyStationReplays(stationId, params)}
              emptyText="아직 내가 진행한 방송 다시듣기가 없어요"
              deleteMode="destroy"
            />
          ) : broadcastStateQuery.isError ? (
            <p className="text-[13px] text-danger">방송 정보를 불러오지 못했어요</p>
          ) : (
            <p className="text-[13px] text-text-muted">불러오는 중...</p>
          )
        )}

        {activeTab === 'savedReplays' && (
          <ReplayCollection
            queryKey="bookmarked-replays"
            fetcher={getBookmarkedReplays}
            emptyText="아직 저장한 방송 다시듣기가 없어요"
            deleteMode="unbookmark"
          />
        )}
      </main>
    </div>
  )
}
