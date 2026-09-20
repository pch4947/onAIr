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
