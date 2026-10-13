import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { getNotifications, markNotificationRead } from '@/shared/api'
import { getNotificationPath } from '@/shared/notificationLink'
import { useCurrentUser } from '@/shared/useCurrentUser'
import type { NotificationItem } from '@/shared/types'

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

type NavKey = 'main' | 'admin' | 'board'

interface SidebarProps {
  active?: NavKey
}

const NAV_ITEMS: { key: NavKey; label: string; icon: string; path: string }[] = [
  { key: 'main', label: '홈 · 방송 선택', icon: '🏠', path: '/main' },
  { key: 'admin', label: '운영자 콘솔', icon: '🛠', path: '/admin' },
  { key: 'board', label: '게시판', icon: '🪧', path: '/board' },
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
      <div className="fixed right-4 top-4 z-50 flex w-80 flex-col gap-2">
        {toasts.map((toast) => (
          <NotificationToast
            key={toast.id}
            notification={toast}
            onClick={() => handleToastClick(toast)}
            onExpire={() => dismissToast(toast.id)}
          />
        ))}
      </div>

      <aside className="flex w-[200px] shrink-0 flex-col gap-7 bg-sidebar px-4 py-6">
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
                  ? 'rounded-lg bg-primary px-3 py-2.5 text-left text-xs font-semibold text-white'
                  : 'rounded-lg px-3 py-2.5 text-left text-xs font-semibold text-nav-inactive'
              }
            >
              {item.icon} {item.label}
            </button>
          ))}
        </nav>
      </aside>
    </>
  )
}
