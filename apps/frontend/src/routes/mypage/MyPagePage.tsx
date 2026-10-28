// 05_MyPage_개인 페이지

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sidebar } from '@/app/Sidebar'
import { AlertIcon, HistoryIcon, BoardIcon } from '@/app/icons'
import { HeartIcon } from '@/routes/board/LikeButton'
import { MyPageTabContent } from '@/routes/mypage/MyPageTabContent'
import { ReplaySaveIcon } from '@/routes/listen/icons'
import { clearAccessToken } from '@/shared/authToken'
import { useCurrentUser } from '@/shared/useCurrentUser'
import { MYPAGE_TABS } from '@/routes/mypage/mock'
import type { MyPageTab } from '@/routes/mypage/mock'
import type { ComponentType } from 'react'

const TAB_ICONS: Record<MyPageTab, ComponentType<{ className?: string }>> = {
  notifications: AlertIcon,
  myPosts: BoardIcon,
  likedPosts: HeartIcon,
  myBroadcastReplays: HistoryIcon,
  savedReplays: ReplaySaveIcon,
}

export function MyPagePage() {
  const navigate = useNavigate()
  const { data: currentUser } = useCurrentUser()
  const [activeTab, setActiveTab] = useState<MyPageTab>('notifications')
  const activeTabConfig = MYPAGE_TABS.find((tab) => tab.id === activeTab) ?? MYPAGE_TABS[0]

  const handleLogout = () => {
    clearAccessToken()
    navigate('/login')
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar active="mypage" />

      <main className="flex-1 bg-surface p-4 pb-24 md:p-8 md:pb-8">
        {/* 모바일: 박스 버튼 허브 */}
        <div className="md:hidden">
          <header className="mb-9">
            <h1 className="text-center text-lg font-semibold text-text">개인 페이지</h1>
            <p className="mt-5 text-2xl font-bold text-text">{currentUser?.name ?? ''}님</p>
            <p className="mt-1.5 flex items-center gap-1 text-base text-text-muted">
              반가워요 <span>👋</span>
            </p>
          </header>

          <div className="grid grid-cols-3 gap-y-5">
            {MYPAGE_TABS.map((tab) => {
              const Icon = TAB_ICONS[tab.id]
              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => navigate(`/mypage/${tab.id}`)}
                  className="flex flex-col items-center gap-2"
                >
                  <span className="flex size-16 items-center justify-center rounded-full bg-field-bg">
                    <Icon className="size-7 text-text" />
                  </span>
                  <span className="text-center text-xs font-medium text-text">{tab.label}</span>
                </button>
              )
            })}
          </div>

          <button
            type="button"
            onClick={handleLogout}
            className="mt-8 w-full rounded-md border border-field-border py-3 text-sm font-semibold text-text-muted"
          >
            로그아웃
          </button>
        </div>

        {/* 데스크톱: 탭 전환 한 페이지 */}
        <div className="hidden md:block">
          <header className="mb-6 flex items-start justify-between">
            <div className="flex flex-col gap-1">
              <h1 className="text-2xl font-semibold text-[#1a1a1f]">개인 페이지</h1>
              <p className="text-[13px] text-text-muted">{activeTabConfig.subtitle}</p>
            </div>
            <button
              type="button"
              onClick={handleLogout}
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

          <MyPageTabContent key={activeTab} tab={activeTab} />
        </div>
      </main>
    </div>
  )
}
