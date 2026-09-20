import { useEffect, useRef } from 'react'
import { RealtimeClient } from '@/realtime/client'
import { useRealtimeStore } from '@/realtime/store'

const WS_URL = import.meta.env.VITE_WS_URL ?? ''

export function useRealtimeConnection() {
  const setConnectionStatus = useRealtimeStore((state) => state.setConnectionStatus)
  const clientRef = useRef<RealtimeClient | null>(null)

  useEffect(() => {
    const client = new RealtimeClient(WS_URL)
    clientRef.current = client

    const unsubscribe = client.onStatusChange(setConnectionStatus)
    client.connect()

    return () => {
      unsubscribe()
      client.close()
      clientRef.current = null
    }
  }, [setConnectionStatus])

  return clientRef
}
