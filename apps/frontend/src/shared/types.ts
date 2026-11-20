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

// 엔진 코너 계약 (docs/ENGINE_REDIS_CONTRACT.md 10.7절)
export type CornerInputMode = 'quote' | 'steer' | 'read' | 'feature' | 'solicit' | 'closed' | 'defer' | 'none'

export interface CurrentCorner {
  corner: string
  title: string
  contract: Partial<Record<'chat' | 'message' | 'song_request' | 'board_story' | 'vote', CornerInputMode>>
}

// 청취자 투표 (#279, 계약 10.8절). 표 수는 창이 닫힌 뒤에만 온다 — 화면에는 띄우지 않는다(DJ가 방송에서 발표)
export interface CurrentVote {
  vote_id: string
  kind: string
  question: string
  options: string[]
  status: 'open' | 'closed'
  opens_at: number
  closes_at: number
  remaining_sec: number
  // /votes/current에만 — 내가 고른 선택지, 안 했으면 null
  mine?: string | null
}

export interface BroadcastStateResponse {
  stationStatus?: 'idle' | 'preparing' | 'started' | 'draining' | 'stopping' | 'cleaning' | 'ended' | 'rejected' | 'unknown'
  stationFormat?: StationFormat | null
  currentCorner?: CurrentCorner | null
  // 방송 중 페르소나 전환(#270)으로 바뀔 수 있다
  djName?: string | null
  currentVote?: CurrentVote | null
  stream: StreamInfo
  stationId: string
  viewerCount: number | null
  pendingRequestCount: number
  oldestRequestAgeSeconds: number
}

export interface RequestRecord {
  id: string
  mine: boolean
  listenerName?: string
  prompt: string
  kind: 'story' | 'song_request' | 'message'
  status: string
  createdAt: string
  trackTitle?: string | null
  trackArtist?: string | null
  trackThumbnailUrl?: string | null
}

export interface QueuedTrackInfo {
  title: string
  artist: string
  duration_ms: number
  track_ref?: string
}

export interface QueuedTrack {
  segment_id: string
  track: QueuedTrackInfo
  // dj: 플레이리스트가 비었을 때 DJ가 장르·주제에 맞춰 고른 곡 (#295)
  source?: 'playlist' | 'request' | 'vote' | 'dj'
  request_id?: string | null
}

export interface NowPlayingTrack extends QueuedTrack {
  started_at: number
}

export interface MusicQueueResponse {
  queue: QueuedTrack[]
  playingSegmentId: string | null
  // 청취자가 지금 듣고 있는 곡. 송출되며 queue에서는 이미 빠졌고, 곡이 끝나면 null (#295)
  nowPlaying?: NowPlayingTrack | null
}

export interface SubmitRequestResponse {
  request: RequestRecord
}

export interface AuthUser {
  id: string
  email: string
  name: string
  createdAt: string
  isGuest?: boolean
  isAdmin?: boolean
  // 사연·신청곡을 보낼 때 "익명으로"의 기본값 (#325)
  defaultAnonymous?: boolean
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

// 청취 화면의 DJ 정보 창 (#353, GET /api/stations/{id}/dj) — 청취자에게 보여 줄 필드만 온다
export interface StationDjInfo {
  dj: {
    name: string
    concept: string | null
    tone: string | null
    style: Partial<PersonaStyle>
    musicTaste: string[]
    signaturePhrases: string[]
    voice: string | null
  }
  format: StationFormat
  topic: string | null
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

export type StationFormat = 'music' | 'talk' | 'news'

// GET /api/stations 한 칸 — 끝난 방송과 빈 채널은 서버가 뺀다
export interface StationListItem {
  station_id: string
  status: 'preparing' | 'started' | 'rejected' | 'draining' | 'stopping' | 'cleaning'
  dj_name: string | null
  topic: string | null
  format: StationFormat
  hls_url: string
  hls_ready: boolean
  listeners: number
  created_at: number
  mine: boolean
}
export type TalkCorner = 'story_corner' | 'text_corner' | 'topic_talk' | 'dj_vs_listeners'
export type NewsSection = 'general' | 'economy' | 'it' | 'sports' | 'culture'
export type Intensity = 'low' | 'mid' | 'high'

export interface StationInfo {
  firstSongUrl?: string
  broadcastMinutes: number
  topic?: string
  format: StationFormat
  corners?: TalkCorner[]
  newsSections?: NewsSection[]
  intensity: Intensity
  // 어드민 테스트 화면의 방송 (#306) — 홈 목록·내 방송·다시듣기에 남지 않는다
  test?: boolean
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
  stationId: string
  startedAt: string
  endedAt: string | null
  durationSeconds: number
  ongoing: boolean
  vodUrl: string
  bookmarkCount: number
  bookmarked: boolean
}

export interface NotificationItem {
  id: string
  kind: string
  title: string
  description: string
  createdAt: string
  isUnread: boolean
  data: { postId?: string }
}
