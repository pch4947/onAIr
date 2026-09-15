// 05_MyPage_개인 페이지

import { useState } from 'react'
import { Sidebar } from '@/app/Sidebar'
import {
  MY_BOARD_POSTS,
  MY_BROADCAST_REPLAYS,
  MYPAGE_TABS,
  NOTIFICATIONS,
  SAVED_REPLAYS,
} from '@/routes/mypage/mock'
import type { ReplayItem } from '@/routes/mypage/mock'
import type { MyPageTab } from '@/routes/mypage/mock'

function ReplayList({ items, thumbnailClassName }: { items: ReplayItem[]; thumbnailClassName: string }) {
  return (
    <ul className="flex flex-col gap-3">
      {items.map((item) => (
        <li
          key={item.id}
          className="flex items-center justify-between gap-4 rounded-md border border-field-border bg-field-bg px-4 py-3.5"
        >
          <div className="flex items-center gap-3.5">
            <div className={`size-12 shrink-0 rounded-md ${thumbnailClassName}`} />
            <div className="flex flex-col gap-1">
              <p className="text-sm font-semibold text-[#1a1a1f]">{item.title}</p>
              <p className="text-xs text-text-muted">{item.meta}</p>
            </div>
          </div>
          <button
            type="button"
            className="shrink-0 rounded-md bg-primary px-3.5 py-2 text-xs font-semibold text-white"
          >
            ▶ 다시보기
          </button>
        </li>
      ))}
    </ul>
  )
}

export function MyPagePage() {
  const [activeTab, setActiveTab] = useState<MyPageTab>('notifications')
  const activeTabConfig = MYPAGE_TABS.find((tab) => tab.id === activeTab) ?? MYPAGE_TABS[0]

  return (
    <div className="flex min-h-screen">
      <Sidebar />

      <main className="flex-1 bg-surface p-8">
        <header className="mb-6 flex flex-col gap-1">
          <h1 className="text-2xl font-semibold text-[#1a1a1f]">개인 페이지</h1>
          <p className="text-[13px] text-text-muted">{activeTabConfig.subtitle}</p>
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

        {activeTab === 'notifications' && (
          <ul className="flex flex-col gap-2.5">
            {NOTIFICATIONS.map((item) => (
              <li
                key={item.id}
                className={
                  item.isUnread
                    ? 'flex items-center justify-between gap-4 rounded-md border border-[#bad1fa] bg-[#ebf2ff] px-4 py-3.5'
                    : 'flex items-center justify-between gap-4 rounded-md border border-field-border bg-field-bg px-4 py-3.5'
                }
              >
                <div className="flex flex-col gap-1">
                  <p className="text-sm font-semibold text-[#1a1a1f]">{item.title}</p>
                  <p className="text-xs text-text-muted">{item.description}</p>
                </div>
                {item.isUnread && <span className="size-2 shrink-0 rounded-full bg-primary" />}
              </li>
            ))}
          </ul>
        )}

        {activeTab === 'myPosts' && (
          <ul className="flex flex-col gap-3">
            {MY_BOARD_POSTS.map((post) => (
              <li
                key={post.id}
                className="flex flex-col gap-1 rounded-md border border-field-border bg-field-bg px-4 py-3.5"
              >
                <p className="text-sm font-semibold text-[#1a1a1f]">{post.title}</p>
                <p className="text-xs text-text-muted">
                  {post.boardName} · {post.timeAgo} · 댓글 {post.commentCount}
                </p>
              </li>
            ))}
          </ul>
        )}

        {activeTab === 'myBroadcastReplays' && (
          <ReplayList items={MY_BROADCAST_REPLAYS} thumbnailClassName="bg-[#292933]" />
        )}

        {activeTab === 'savedReplays' && (
          <ReplayList items={SAVED_REPLAYS} thumbnailClassName="bg-[#33384d]" />
        )}
      </main>
    </div>
  )
}
