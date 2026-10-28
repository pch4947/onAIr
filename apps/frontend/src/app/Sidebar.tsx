import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { getNotifications, markNotificationRead } from '@/shared/api'
import { getNotificationPath } from '@/shared/notificationLink'
import { useCurrentUser } from '@/shared/useCurrentUser'
import { BoardIcon, HomeIcon, UserIcon } from '@/app/icons'
import type { NotificationItem } from '@/shared/types'
import type { ComponentType } from 'react'

const TOAST_DURATION_MS = 5000
const TOAST_TRANSITION_MS = 200

interface NotificationToastProps {
  notification: NotificationItem
  onClick: () => void
  onExpire: () => void
}

function NotificationToast({ notification, onClick, onExpire }: NotificationToastProps) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const show = requestAnimationFrame(() => setVisible(true))
    const hide = window.setTimeout(() => setVisible(false), TOAST_DURATION_MS - TOAST_TRANSITION_MS)
    return () => {
      cancelAnimationFrame(show)
      window.clearTimeout(hide)
    }
  }, [])

  return (
    <button
      type="button"
      onClick={onClick}
      onTransitionEnd={() => {
        if (!visible) onExpire()
      }}
      className={
        (visible ? 'translate-y-0 opacity-100' : '-translate-y-2 opacity-0') +
        ' flex flex-col gap-1 rounded-md border border-[#bad1fa] bg-[#ebf2ff] px-4 py-3.5 text-left shadow-md transition-all duration-200 ease-out'
      }
    >
      <p className="text-sm font-semibold text-[#1a1a1f]">{notification.title}</p>
      <p className="text-xs text-text-muted">{notification.description}</p>
    </button>
  )
}

type NavKey = 'main' | 'board' | 'mypage'

interface SidebarProps {
  active?: NavKey
}

type NavItem = { key: NavKey; label: string; icon: ComponentType<{ className?: string }>; path: string }

const NAV_ITEMS: NavItem[] = [
  { key: 'main', label: '홈 · 방송 선택', icon: HomeIcon, path: '/main' },
  { key: 'board', label: '게시판', icon: BoardIcon, path: '/board' },
]

const MOBILE_NAV_ITEMS: NavItem[] = [
  { key: 'board', label: '게시판', icon: BoardIcon, path: '/board' },
  { key: 'main', label: '홈', icon: HomeIcon, path: '/main' },
  { key: 'mypage', label: '마이페이지', icon: UserIcon, path: '/mypage' },
]

export function Sidebar({ active }: SidebarProps) {
  const navigate = useNavigate()
  const { data: user } = useCurrentUser()
  const name = user?.name ?? ''
  const [toasts, setToasts] = useState<NotificationItem[]>([])
  const seenIds = useRef<Set<string> | null>(null)
  const notificationsQuery = useQuery({
    queryKey: ['notifications-unread'],
    queryFn: () => getNotifications({ limit: 5 }),
    enabled: Boolean(user),
    refetchInterval: 5000,
  })
  const unreadCount = notificationsQuery.data?.unreadCount ?? 0

  useEffect(() => {
    const notifications = notificationsQuery.data?.notifications
    if (!notifications) return
    if (seenIds.current === null) {
      // 처음 불러올 때 이미 있던 알림은 팝업으로 보여주지 않고, 그 이후 새로 생긴 것만 보여준다.
      seenIds.current = new Set(notifications.map((item) => item.id))
      return
    }
    const fresh = notifications.filter((item) => item.isUnread && !seenIds.current!.has(item.id))
    if (fresh.length === 0) return
    fresh.forEach((item) => seenIds.current!.add(item.id))
    setToasts((prev) => [...prev, ...fresh])
  }, [notificationsQuery.data])

  const dismissToast = (id: string) => setToasts((prev) => prev.filter((toast) => toast.id !== id))

  const handleToastClick = (notification: NotificationItem) => {
    dismissToast(notification.id)
    markNotificationRead(notification.id).catch(() => {})
    navigate(getNotificationPath(notification))
  }

  return (
    <>
      <div className="fixed inset-x-4 top-4 z-50 flex flex-col gap-2 md:inset-x-auto md:right-4 md:w-80">
        {toasts.map((toast) => (
          <NotificationToast
            key={toast.id}
            notification={toast}
            onClick={() => handleToastClick(toast)}
            onExpire={() => dismissToast(toast.id)}
          />
        ))}
      </div>

      {/* 데스크톱: 고정폭 좌측 사이드바 */}
      <aside className="hidden w-50 shrink-0 flex-col gap-7 bg-sidebar px-4 py-6 md:flex">
        <button
          type="button"
          onClick={() => navigate('/mypage')}
          className="flex items-center gap-2 text-left"
        >
          <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-primary-strong text-xs font-semibold text-white">
            {name.charAt(0) || '?'}
          </div>
          <p className="truncate text-sm font-semibold text-white">{name}</p>
          {unreadCount > 0 && (
            <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-white text-[10px] font-bold leading-none text-primary">
              {unreadCount > 99 ? '99+' : unreadCount}
            </span>
          )}
        </button>

        <nav className="flex flex-col gap-1">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => navigate(item.path)}
              className={
                active === item.key
                  ? 'flex items-center gap-2 rounded-lg bg-primary px-3 py-2.5 text-left text-xs font-semibold text-white'
                  : 'flex items-center gap-2 rounded-lg px-3 py-2.5 text-left text-xs font-semibold text-nav-inactive'
              }
            >
              <item.icon className="size-4.5" />
              {item.label}
            </button>
          ))}
        </nav>
      </aside>

      {/* 모바일: 하단 탭 바 */}
      <nav className="fixed inset-x-0 bottom-0 z-40 flex h-16 items-center justify-around bg-linear-to-r from-[#6f387b]/90 via-[#3f327a]/90 to-[#17727b]/90 px-1 backdrop-blur-md md:hidden">
        {MOBILE_NAV_ITEMS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => navigate(item.path)}
            className="relative flex flex-1 flex-col items-center gap-0.5 py-1"
          >
            {/* 로고 PNG는 상하 투명 여백이 커서 보이는 높이(약 26px)만 박스로 잡고 나머지는 넘치게 둔다 */}
            <span className="flex h-7 w-12 items-center justify-center">
              {item.key === 'main' ? (
                <img src="/logo_icon.png" alt="" className="size-12 shrink-0" />
              ) : (
                <item.icon className={active === item.key ? 'size-5 text-white' : 'size-5 text-nav-inactive'} />
              )}
            </span>
            <span className={active === item.key ? 'text-[10px] font-semibold text-white' : 'text-[10px] font-medium text-nav-inactive'}>
              {item.label}
            </span>
            {item.key === 'mypage' && unreadCount > 0 && (
              <span className="absolute right-3 top-0 flex size-4 shrink-0 items-center justify-center rounded-full bg-white text-[9px] font-bold leading-none text-primary">
                {unreadCount > 99 ? '99+' : unreadCount}
              </span>
            )}
          </button>
        ))}
      </nav>
    </>
  )
}
