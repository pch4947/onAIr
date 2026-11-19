// 설정 페이지 (#324) — 데스크톱은 사이드바 맨 아래, 모바일은 마이페이지에서 들어온다
import { useNavigate } from 'react-router-dom'
import { Sidebar } from '@/app/Sidebar'
import { SettingsPanel } from '@/routes/settings/SettingsPanel'

export function SettingsPage() {
  const navigate = useNavigate()
  return (
    <div className="flex min-h-screen">
      <Sidebar active="settings" />

      <main className="flex-1 bg-surface p-4 pb-24 md:p-8 md:pb-8">
        <header className="mb-6 flex flex-col gap-1">
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
      </main>
    </div>
  )
}
