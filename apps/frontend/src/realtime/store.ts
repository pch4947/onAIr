import { create } from 'zustand'
import type { ConnectionStatus } from '@/realtime/client'

interface RealtimeState {
  connectionStatus: ConnectionStatus
  setConnectionStatus: (status: ConnectionStatus) => void
}

export const useRealtimeStore = create<RealtimeState>((set) => ({
  connectionStatus: 'disconnected',
  setConnectionStatus: (status) => set({ connectionStatus: status }),
}))

// 웹소켓이 붙어 있는 동안의 폴링 주기 — 바뀌면 웹소켓이 알려 주므로, 놓친 것을 메우는 정도로만 묻는다
const CONNECTED_POLL_INTERVAL_MS = 15000

// 웹소켓이 붙어 있으면 느리게, 끊기면 원래 주기로 폴링한다 (#312)
export function useRealtimePollInterval(disconnectedMs: number): number {
  const connected = useRealtimeStore((state) => state.connectionStatus === 'connected')
  return connected ? Math.max(disconnectedMs, CONNECTED_POLL_INTERVAL_MS) : disconnectedMs
}
