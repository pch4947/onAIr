// 05_MyPage_개인 페이지 — 탭별 세부 페이지

import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { Sidebar } from '@/app/Sidebar'
import { MyPageTabContent } from '@/routes/mypage/MyPageTabContent'
import { clearAccessToken } from '@/shared/authToken'
import { MYPAGE_TABS } from '@/routes/mypage/mock'

export function MyPageTabPage() {
  const navigate = useNavigate()
  const { tab } = useParams<{ tab: string }>()
  const activeTabConfig = MYPAGE_TABS.find((item) => item.id === tab)

  if (!activeTabConfig) {
    return <Navigate to="/mypage" replace />
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar active="mypage" />

      <main className="flex-1 bg-surface p-4 pb-24 md:p-8 md:pb-8">
        <header className="mb-6 flex items-start justify-between">
          <div className="flex flex-col gap-1">
            <button
              type="button"
              onClick={() => navigate('/mypage')}
              aria-label="개인 페이지로 돌아가기"
              className="mb-1 self-start text-2xl font-semibold text-text-muted"
            >
              ←
            </button>
            <h1 className="text-lg font-semibold text-text md:text-2xl">{activeTabConfig.label}</h1>
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

        <MyPageTabContent key={activeTabConfig.id} tab={activeTabConfig.id} />
      </main>
    </div>
  )
}
