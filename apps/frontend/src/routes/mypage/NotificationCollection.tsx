import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  deleteAllNotifications,
  deleteNotification,
  getNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from '@/shared/api'
import { getNotificationPath } from '@/shared/notificationLink'
import type { NotificationItem } from '@/shared/types'

const PAGE_SIZE = 10

type NotificationPage = { notifications: NotificationItem[]; total: number; unreadCount: number }

export function NotificationCollection() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [confirmDeleteAll, setConfirmDeleteAll] = useState(false)
  const queryKey = ['notifications', page]
  const query = useQuery({
    queryKey,
    queryFn: () => getNotifications({ offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE }),
  })
  const notifications = query.data?.notifications ?? []
  const totalPages = Math.max(1, Math.ceil((query.data?.total ?? 0) / PAGE_SIZE))
  const unreadCount = query.data?.unreadCount ?? 0

  // 알림을 지우면 뒤 페이지가 당겨지므로 다시 불러온다. 사이드바의 안 읽은 알림 수도 바로 맞춘다 (#366)
  const syncAfterChange = () => {
    void queryClient.invalidateQueries({ queryKey: ['notifications'] })
    void queryClient.invalidateQueries({ queryKey: ['notifications-unread'] })
  }

  // 지운 뒤 목록이 짧아져 지금 페이지가 사라졌으면 마지막 페이지로 옮긴다
  if (query.data && page > totalPages) setPage(totalPages)

  const handleClick = (item: NotificationItem) => {
    if (item.isUnread) {
      queryClient.setQueryData<NotificationPage>(queryKey, (old) => old && {
        ...old,
        notifications: old.notifications.map((n) => (n.id === item.id ? { ...n, isUnread: false } : n)),
        unreadCount: Math.max(0, old.unreadCount - 1),
      })
      markNotificationRead(item.id).then(syncAfterChange, () => query.refetch())
    }
    // 갈 곳이 따로 없는 알림(사연 읽힘 등)은 이 목록이 그 알림의 자리다 — 데스크톱에서 모바일용 하위 페이지로 넘어가지 않게 한다
    const path = getNotificationPath(item)
    if (path !== '/mypage/notifications') navigate(path)
  }

  const handleReadAll = () => {
    queryClient.setQueryData<NotificationPage>(queryKey, (old) => old && {
      ...old,
      notifications: old.notifications.map((n) => ({ ...n, isUnread: false })),
      unreadCount: 0,
    })
    markAllNotificationsRead().then(syncAfterChange, () => query.refetch())
  }

  const handleDelete = (event: React.MouseEvent, item: NotificationItem) => {
    event.stopPropagation()
    queryClient.setQueryData<NotificationPage>(queryKey, (old) => old && {
      ...old,
      notifications: old.notifications.filter((n) => n.id !== item.id),
      total: Math.max(0, old.total - 1),
      unreadCount: item.isUnread ? Math.max(0, old.unreadCount - 1) : old.unreadCount,
    })
    if (notifications.length === 1 && page > 1) setPage(page - 1)
    deleteNotification(item.id).then(syncAfterChange, () => query.refetch())
  }

  const handleDeleteAll = () => {
    // 모든 페이지의 캐시를 비운다 — 첫 페이지로 돌아갈 때 지운 알림이 잠깐 보이지 않게
    queryClient.setQueriesData<NotificationPage>({ queryKey: ['notifications'] }, (old) => old && {
      ...old, notifications: [], total: 0, unreadCount: 0,
    })
    setConfirmDeleteAll(false)
    setPage(1)
    deleteAllNotifications().then(syncAfterChange, () => query.refetch())
  }

  if (query.isError) {
    return <p className="text-[13px] text-danger">알림을 불러오지 못했어요</p>
  }

  // 빈 목록은 첫 페이지에서만 "없어요"다 — 뒤 페이지가 비었으면 위에서 앞 페이지로 옮긴다
  if (!query.isLoading && notifications.length === 0 && page === 1) {
    return <p className="text-[13px] text-text-muted">아직 알림이 없어요</p>
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end gap-3">
        {unreadCount > 0 && (
          <button type="button" onClick={handleReadAll} className="text-xs font-medium text-primary">
            모두 읽음 처리
          </button>
        )}
        {notifications.length > 0 && (
          <button
            type="button"
            onClick={() => setConfirmDeleteAll(true)}
            className="text-xs font-medium text-text-muted"
          >
            전체 삭제
          </button>
        )}
      </div>

      <ul className="flex flex-col gap-2.5">
        {notifications.map((item) => (
          <li
            key={item.id}
            onClick={() => handleClick(item)}
            className={
              item.isUnread
                ? 'flex cursor-pointer items-center justify-between gap-4 rounded-md border border-primary-soft-border bg-primary-soft px-4 py-3.5'
                : 'flex cursor-pointer items-center justify-between gap-4 rounded-md border border-field-border bg-field-bg px-4 py-3.5'
            }
          >
            <div className="flex flex-col gap-1">
              <p className="text-sm font-semibold text-[#1a1a1f]">{item.title}</p>
              <p className="text-xs text-text-muted">{item.description}</p>
            </div>
            <div className="flex shrink-0 items-center gap-2.5">
              {item.isUnread && <span className="size-2 shrink-0 rounded-full bg-primary" />}
              <button
                type="button"
                onClick={(event) => handleDelete(event, item)}
                aria-label="알림 삭제"
                className="flex size-5 items-center justify-center rounded-full text-sm text-text-muted hover:bg-field-border"
              >
                ×
              </button>
            </div>
          </li>
        ))}
      </ul>

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-3">
          <button
            type="button"
            onClick={() => setPage((prev) => Math.max(1, prev - 1))}
            disabled={page <= 1}
            className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
          >
            이전
          </button>
          <span className="text-xs text-text-muted">
            {page} / {totalPages}
          </span>
          <button
            type="button"
            onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
            disabled={page >= totalPages}
            className="rounded-md border border-field-border px-3.5 py-2 text-xs font-semibold text-text-muted disabled:opacity-40"
          >
            다음
          </button>
        </div>
      )}

      {confirmDeleteAll && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
          <div className="flex w-full max-w-100 flex-col gap-4 rounded-xl bg-surface px-7 pb-6 pt-7 shadow-lg">
            <p className="text-lg font-semibold text-text">알림을 전체 삭제할까요?</p>
            <p className="text-[13px] leading-relaxed text-text-muted">
              삭제한 알림은 되돌릴 수 없어요.
            </p>
            <footer className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmDeleteAll(false)}
                className="rounded-md px-4 py-2.5 text-[13px] font-medium text-text-muted"
              >
                취소
              </button>
              <button
                type="button"
                onClick={handleDeleteAll}
                className="rounded-md bg-danger px-5 py-2.5 text-[13px] font-semibold text-white"
              >
                전체 삭제
              </button>
            </footer>
          </div>
        </div>
      )}
    </div>
  )
}
