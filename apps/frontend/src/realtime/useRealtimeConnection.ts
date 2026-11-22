import { useEffect } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { getAccessToken } from '@/shared/authToken'
import type { RequestRecord } from '@/shared/types'
import { RealtimeClient, type RealtimeMessage } from '@/realtime/client'
import { useRealtimeStore } from '@/realtime/store'

// 백엔드 주소에서 웹소켓 주소를 만든다 (http → ws, https → wss)
function socketUrl(stationId: string): string {
  const url = new URL(`/ws/stations/${encodeURIComponent(stationId)}`,
    import.meta.env.VITE_API_BASE_URL || window.location.origin)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.href
}

// 채팅/요청 목록을 쓰는 화면들 — 청취 화면, 테스트 패널
const REQUEST_LIST_KEYS = ['requests', 'test-requests']

// changed 알림의 항목 → 다시 받을 쿼리
const TOPIC_QUERY_KEYS: Record<string, string[]> = {
  broadcast: ['broadcast-state', 'owned-station'],
  requests: REQUEST_LIST_KEYS,
  queue: ['music-queue'],
  vote: ['vote'],
}

// 받은 요청을 목록에 바로 넣는다 — 다시 받지 않는다. 있던 것은 바꾸고 새것은 더해 서버와 같은 순서(작성 시각, ID)로 둔다
function mergeRequests(cache: QueryClient, stationId: string, incoming: RequestRecord[]) {
  for (const key of REQUEST_LIST_KEYS) {
    cache.setQueriesData<{ requests: RequestRecord[] }>({ queryKey: [key, stationId] }, (current) => {
      if (!current) return current
      const byId = new Map(current.requests.map((item) => [item.id, item]))
      for (const item of incoming) byId.set(item.id, item)
      const requests = [...byId.values()].sort((a, b) =>
        a.createdAt === b.createdAt ? a.id.localeCompare(b.id) : a.createdAt.localeCompare(b.createdAt))
      return { requests }
    })
  }
}

// 방송별 웹소켓 (#312). 바뀐 채팅/요청은 데이터째 받아 목록에 넣고, 나머지는 바뀐 항목만 다시 받는다
export function useRealtimeConnection(stationId: string | undefined) {
  const setConnectionStatus = useRealtimeStore((state) => state.setConnectionStatus)
  const cache = useQueryClient()

  useEffect(() => {
    if (!stationId) return
    const client = new RealtimeClient(socketUrl(stationId), getAccessToken)
    const unsubscribes = [
      client.onStatusChange(setConnectionStatus),
      client.on('requests', (message: RealtimeMessage) => {
        mergeRequests(cache, stationId, message.requests as RequestRecord[])
      }),
      client.on('changed', (message: RealtimeMessage) => {
        for (const topic of message.topics as string[]) {
          for (const key of TOPIC_QUERY_KEYS[topic] ?? []) {
            void cache.invalidateQueries({ queryKey: [key, stationId] })
          }
        }
      }),
    ]
    client.connect()
    const wake = () => { if (document.visibilityState === 'visible') client.wake() }
    document.addEventListener('visibilitychange', wake)
    window.addEventListener('online', wake)
    return () => {
      document.removeEventListener('visibilitychange', wake)
      window.removeEventListener('online', wake)
      // 닫고 나서 구독을 끊는다 — close()가 알리는 'disconnected'를 받아야 전역 상태가 '연결됨'으로 남지 않는다.
      // 남으면 다른 화면이 소켓 없이도 느린 조회 간격(15초)을 쓴다 (#366)
      client.close()
      unsubscribes.forEach((unsubscribe) => unsubscribe())
    }
  }, [stationId, cache, setConnectionStatus])
}
