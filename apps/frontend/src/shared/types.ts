export type StreamStatus = 'bootstrapping' | 'ready' | 'planning'

export interface StreamInfo {
  status: StreamStatus
  bufferSeconds: number
  currentSegment: unknown | null
  updatedAt: string
  hlsUrl?: string
  error?: string | null
}

export interface BroadcastStateResponse {
  stream: StreamInfo
  pendingRequestCount: number
  oldestRequestAgeSeconds: number
}

export interface RequestRecord {
  id: string
  listenerId: string
  prompt: string
  status: string
  createdAt: string
}

export interface SubmitRequestResponse {
  request: RequestRecord
}
