// 05_MyPage_개인 페이지

import { useState } from 'react'
import { Sidebar } from '@/app/Sidebar'
import { MYPAGE_TABS, NOTIFICATIONS } from '@/routes/mypage/mock'
import type { MyPageTab } from '@/routes/mypage/mock'

export function MyPagePage() {
  const [activeTab, setActiveTab] = useState<MyPageTab>('notifications')

  return (
    <div className="flex min-h-screen">
      <Sidebar />

      <main className="flex-1 bg-surface p-8">
        <header className="mb-6 flex flex-col gap-1">
          <h1 className="text-2xl font-semibold text-[#1a1a1f]">개인 페이지</h1>
          <p className="text-[13px] text-text-muted">
            내가 쓴 사연이 방송에 소개되는 순간을 실시간으로 알려드려요
          </p>
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

        {activeTab === 'notifications' ? (
          <ul className="flex flex-col gap-2.5">
            {NOTIFICATIONS.map((item) => (
              <li
                key={item.id}
                className={
                  item.isUnread
                    ? 'flex items-center justify-between gap-4 rounded-md border border-[#bad1fa] bg-[#ebf2ff] px-4 py-3.5'
                    : 'flex items-center justify-between gap-4 rounded-md border border-[#e0e0e0] bg-field-bg px-4 py-3.5'
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
        ) : (
          <p className="text-sm text-text-muted">준비 중입니다.</p>
        )}
      </main>
    </div>
  )
}
