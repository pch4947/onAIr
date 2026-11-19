// 설정 페이지 (#324, #325) — 데스크톱은 사이드바 맨 아래, 모바일은 마이페이지에서 들어온다
import { useNavigate } from 'react-router-dom'
import { Sidebar } from '@/app/Sidebar'
import { AccountSection } from '@/routes/settings/AccountSection'
import { SettingsPanel } from '@/routes/settings/SettingsPanel'
import { useCurrentUser } from '@/shared/useCurrentUser'

export function SettingsPage() {
  const navigate = useNavigate()
  const { data: user } = useCurrentUser()
  // 게스트는 로그아웃하면 계정이 지워지고, 어드민은 핀으로 들어오는 공용 계정이라 계정 설정이 없다
  const isMember = Boolean(user && !user.isGuest && !user.isAdmin)
  return (
    <div className="flex min-h-screen">
      <Sidebar active="settings" />

      <main className="flex flex-1 flex-col gap-8 bg-surface p-4 pb-24 md:p-8 md:pb-8">
        <header className="flex flex-col gap-1">
          <button
            type="button"
            onClick={() => navigate('/mypage')}
            aria-label="개인 페이지로 돌아가기"
            className="mb-1 self-start text-2xl font-semibold text-text-muted md:hidden"
          >
            ←
          </button>
          <h1 className="text-lg font-semibold text-text md:text-2xl">설정</h1>
        </header>

        <SettingsPanel />
        {isMember && user && <AccountSection user={user} />}
      </main>
    </div>
  )
}
