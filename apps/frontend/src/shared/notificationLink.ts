import type { NotificationItem } from '@/shared/types'

/** Where clicking a notification should take the user — the page the notification is about. */
export function getNotificationPath(notification: NotificationItem): string {
  if (notification.kind === 'comment' && notification.data.postId) {
    return `/board?post=${notification.data.postId}`
  }
  return '/mypage'
}
