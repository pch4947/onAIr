import { useNavigate } from 'react-router-dom'
import { CURRENT_USER } from '@/routes/mypage/mock'

type NavKey = 'main' | 'admin' | 'board'

interface SidebarProps {
  active?: NavKey
}

const NAV_ITEMS: { key: NavKey; label: string; icon: string; path: string }[] = [
  { key: 'main', label: '홈 · 방송 선택', icon: '🏠', path: '/' },
  { key: 'admin', label: '운영자 콘솔', icon: '🛠', path: '/admin' },
  { key: 'board', label: '게시판', icon: '🪧', path: '/board' },
]

export function Sidebar({ active }: SidebarProps) {
  const navigate = useNavigate()

  return (
    <aside className="flex w-[200px] shrink-0 flex-col gap-7 bg-sidebar px-4 py-6">
      <button
        type="button"
        onClick={() => navigate('/mypage')}
        className="flex items-center gap-2 text-left"
      >
        <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-primary-strong text-xs font-semibold text-white">
          {CURRENT_USER.avatarInitial}
        </div>
        <p className="truncate text-sm font-semibold text-white">{CURRENT_USER.name}</p>
      </button>

      <nav className="flex flex-col gap-1">
        {NAV_ITEMS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => navigate(item.path)}
            className={
              active === item.key
                ? 'rounded-lg bg-primary px-3 py-2.5 text-left text-xs font-semibold text-white'
                : 'rounded-lg px-3 py-2.5 text-left text-xs font-semibold text-nav-inactive'
            }
          >
            {item.icon} {item.label}
          </button>
        ))}
      </nav>
    </aside>
  )
}
