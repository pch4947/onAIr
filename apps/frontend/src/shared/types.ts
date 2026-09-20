export type StreamStatus = 'bootstrapping' | 'ready' | 'planning'

export interface StreamInfo {
  status: StreamStatus
  bufferSeconds: number
  currentSegment: unknown | null
  updatedAt: string
}

export interface BroadcastStateResponse {
  stream: StreamInfo
  pendingRequestCount: number
  oldestRequestAgeSeconds: number
}
