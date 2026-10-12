export type StreamStatus = 'bootstrapping' | 'ready' | 'planning'

export interface StreamInfo {
  status: StreamStatus
  bufferSeconds: number
  currentSegment: unknown | null
  updatedAt: string
  hlsUrl?: string
  sessionId?: string
  error?: string | null
}

export interface BroadcastStateResponse {
  stream: StreamInfo
  stationId: string
  viewerCount: number | null
  pendingRequestCount: number
  oldestRequestAgeSeconds: number
}

export interface RequestRecord {
  id: string
  listenerId: string
  prompt: string
  kind: 'story' | 'mood'
  status: string
  createdAt: string
}

export interface SubmitRequestResponse {
  request: RequestRecord
}

export interface AuthUser {
  id: string
  email: string
  name: string
  createdAt: string
}

export interface AuthResponse {
  user: AuthUser
  accessToken: string
}

export type Formality = 'polite' | 'casual'
export type Energy = 'low' | 'mid' | 'high'
export type Humor = 'rare' | 'some' | 'often'

export interface PersonaStyle {
  formality: Formality
  energy: Energy
  humor: Humor
}

export interface PersonaForm {
  style: PersonaStyle
  musicTaste: string[]
  voice: string
  djName?: string
  hostNote?: string
}

export interface Persona {
  style: PersonaStyle
  musicTaste: string[]
  voice: string
  djName: string
  concept: string
  tone: string
  examples: string[]
  signaturePhrases: string[]
  forbidden: string[]
}

export interface Voice {
  id: string
  name: string
  gender: string
  description: string
}

export interface StationInfo {
  firstSongUrl?: string
  broadcastMinutes: number
  topic?: string
}

export interface PersonaFieldError {
  field: string
  reason: string
}

export interface BoardPost {
  id: string
  authorId: string
  authorName: string
  title: string
  body: string
  songRequestUrl: string | null
  createdAt: string
  likeCount: number
  liked: boolean
}

export interface BoardComment {
  id: string
  postId: string
  authorId: string
  authorName: string
  body: string
  createdAt: string
  likeCount: number
  liked: boolean
}

export interface LikeStatus {
  likeCount: number
  liked: boolean
}

export interface ReplaySession {
  id: string
  startedAt: string
  endedAt: string | null
  durationSeconds: number
  ongoing: boolean
  vodUrl: string
  bookmarkCount: number
  bookmarked: boolean
}
