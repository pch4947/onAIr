// 내 채팅·사연·신청곡이 방송에 나간 순간을 잡아 화면 알림으로 만든다 (#324). 설정에서 끌 수 있다
import { useEffect, useRef, useState } from 'react'
import type { RequestRecord } from '@/shared/types'

const TOAST_MS = 4000
// v2 채팅·사연은 reflected, 신청곡과 v1은 곡·멘트가 끝까지 나간 played
const AIRED = new Set(['reflected', 'played'])

const TITLE: Record<RequestRecord['kind'], string> = {
  story: '내 채팅이 방송에 나왔어요',
  message: '내 사연이 방송에 나왔어요',
  song_request: '내 신청곡이 방송에 나왔어요',
}

export interface AiredToast {
  id: string
  title: string
  detail: string
}

export function useAiredToasts(requests: RequestRecord[] | undefined, enabled: boolean) {
  const [toasts, setToasts] = useState<AiredToast[]>([])
  const seen = useRef<Map<string, string> | null>(null)

  useEffect(() => {
    if (!requests) return
    const previous = seen.current
    seen.current = new Map(requests.map((item) => [item.id, item.status]))
    // 처음 받은 목록은 이미 지난 일이다 — 이후에 방송된 것만 알린다
    if (!previous || !enabled) return
    const fresh = requests.filter((item) =>
      item.mine && AIRED.has(item.status) && !AIRED.has(previous.get(item.id) ?? ''))
    if (!fresh.length) return
    setToasts((prev) => [...prev, ...fresh.map((item) => ({
      id: `${item.id}-${item.status}`,
      title: TITLE[item.kind] ?? '내 요청이 방송에 나왔어요',
      detail: item.kind === 'song_request' && item.trackTitle ? item.trackTitle : item.prompt,
    }))])
  }, [requests, enabled])

  useEffect(() => {
    if (!toasts.length) return
    const timer = setTimeout(() => setToasts((prev) => prev.slice(1)), TOAST_MS)
    return () => clearTimeout(timer)
  }, [toasts])

  const dismiss = (id: string) => setToasts((prev) => prev.filter((toast) => toast.id !== id))
  return { toasts, dismiss }
}
