import { getAccessToken } from '@/shared/authToken'
import type {
  AuthResponse,
  AuthUser,
  BoardComment,
  BoardPost,
  BroadcastStateResponse,
  LikeStatus,
  MusicQueueResponse,
  NotificationItem,
  ReplaySession,
  RequestRecord,
  StationListItem,
  SubmitRequestResponse,
} from '@/shared/types'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

// 다중 방송(#238): 방송별 경로만 쓴다. 방송 ID 없는 옛 경로(/api/stream/state 등)는 백엔드가 지운다
function stationPath(stationId: string, path: string) {
  return `/api/stations/${encodeURIComponent(stationId)}${path}`
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function getStations(): Promise<{ stations: StationListItem[] }> {
  const res = await fetch(`${API_BASE}/api/stations`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new ApiError(res.status, `GET /api/stations failed: ${res.status}`)
  }
  return res.json() as Promise<{ stations: StationListItem[] }>
}

export async function getBroadcastState(stationId: string): Promise<BroadcastStateResponse> {
  const path = stationPath(stationId, '/stream/state')
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) {
    throw new ApiError(res.status, `GET ${path} failed: ${res.status}`)
  }
  return res.json() as Promise<BroadcastStateResponse>
}

// 테스트 화면 — 어드민만. 202는 요청 접수이고 실제 전환은 엔진이 한다
export async function skipCorner(stationId: string, corner: string): Promise<{ corner: string; discarded: number }> {
  const res = await fetch(`${API_BASE}/api/stations/${encodeURIComponent(stationId)}/corner-skip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getAccessToken() ?? ''}` },
    body: JSON.stringify({ corner }),
  })
  if (!res.ok) {
    throw new Error(`CORNER_SKIP_${res.status}`)
  }
  return res.json() as Promise<{ corner: string; discarded: number }>
}

// 엔진 패널용 이벤트 (#208) — summary는 엔진이 만든 한 줄, data는 종류별 원자료
export type EngineEventKind =
  | 'generation' | 'corner' | 'station' | 'input' | 'selection' | 'steer' | 'aggregate'
  | 'story_judge' | 'topic_plan' | 'callback' | 'track' | 'rss' | 'aired'

export interface EngineEvent {
  id: number
  at: number
  kind: EngineEventKind | string
  summary: string
  data: Record<string, unknown> | null
}

// 테스트 화면 — 어드민만. 엔진의 판단 흐름(입력 라우팅·채팅 선별·생성 단계·코너·송출 등)
export async function getEngineEvents(stationId: string, after?: number | null): Promise<{ events: EngineEvent[]; next: number | null }> {
  const query = after == null ? '' : `?after=${after}`
  const res = await fetch(`${API_BASE}/api/stations/${encodeURIComponent(stationId)}/events${query}`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`GET events failed: ${res.status}`)
  }
  return res.json() as Promise<{ events: EngineEvent[]; next: number | null }>
}

export async function sendPresenceHeartbeat(stationId: string, clientId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/stations/${stationId}/presence`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clientId }),
  })
  if (!res.ok) {
    throw new Error(`POST /api/stations/${stationId}/presence failed: ${res.status}`)
  }
}

export async function submitRequest(input: {
  stationId: string
  prompt: string
  listenerName?: string
  kind?: 'story' | 'message'
  trackTitle?: string
  trackArtist?: string
  trackThumbnailUrl?: string
}): Promise<SubmitRequestResponse> {
  const res = await fetch(`${API_BASE}${stationPath(input.stationId, '/requests')}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getAccessToken() ?? ''}` },
    body: JSON.stringify({
      prompt: input.prompt, listenerName: input.listenerName,
      kind: input.kind, trackTitle: input.trackTitle, trackArtist: input.trackArtist,
      trackThumbnailUrl: input.trackThumbnailUrl,
    }),
  })
  if (res.status === 401) {
    throw new Error('UNAUTHORIZED')
  }
  if (!res.ok) {
    throw new Error(`POST /api/requests failed: ${res.status}`)
  }
  return res.json() as Promise<SubmitRequestResponse>
}

export async function getRequests(stationId: string): Promise<{ requests: RequestRecord[] }> {
  const res = await fetch(`${API_BASE}${stationPath(stationId, '/requests')}`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`GET /api/requests failed: ${res.status}`)
  }
  return res.json() as Promise<{ requests: RequestRecord[] }>
}

export async function getMusicQueue(stationId: string): Promise<MusicQueueResponse> {
  const res = await fetch(`${API_BASE}/api/stations/${encodeURIComponent(stationId)}/tracks/queue`)
  if (!res.ok) {
    throw new Error(`GET /api/stations/${stationId}/tracks/queue failed: ${res.status}`)
  }
  return res.json() as Promise<MusicQueueResponse>
}

export async function signup(email: string, password: string, name: string): Promise<AuthResponse> {
  const res = await fetch(`${API_BASE}/api/auth/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, name }),
  })
  if (res.status === 409) {
    const body = (await res.json().catch(() => null)) as { detail?: string } | null
    throw new Error(body?.detail?.toLowerCase().includes('name') ? 'NAME_TAKEN' : 'EMAIL_TAKEN')
  }
  if (!res.ok) {
    throw new Error(`POST /api/auth/signup failed: ${res.status}`)
  }
  return res.json() as Promise<AuthResponse>
}

export async function getMe(): Promise<AuthUser> {
  const res = await fetch(`${API_BASE}/api/auth/me`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (res.status === 401) {
    throw new Error('UNAUTHORIZED')
  }
  if (!res.ok) {
    throw new Error(`GET /api/auth/me failed: ${res.status}`)
  }
  const { user } = (await res.json()) as { user: AuthUser }
  return user
}

export async function login(email: string, password: string): Promise<AuthResponse> {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (res.status === 401) {
    throw new Error('INVALID_CREDENTIALS')
  }
  if (!res.ok) {
    throw new Error(`POST /api/auth/login failed: ${res.status}`)
  }
  return res.json() as Promise<AuthResponse>
}

// 게스트 계정이면 서버가 계정을 지운다. 일반 계정은 서버에서 하는 일이 없다
export async function logoutSession(): Promise<void> {
  const res = await fetch(`${API_BASE}/api/auth/logout`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`POST /api/auth/logout failed: ${res.status}`)
  }
}

export async function joinAsGuest(name: string): Promise<AuthResponse> {
  const res = await fetch(`${API_BASE}/api/auth/guest`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (res.status === 409) {
    throw new Error('NAME_TAKEN')
  }
  if (!res.ok) {
    throw new Error(`POST /api/auth/guest failed: ${res.status}`)
  }
  return res.json() as Promise<AuthResponse>
}

export async function joinAsAdmin(pin: string): Promise<AuthResponse> {
  const res = await fetch(`${API_BASE}/api/auth/admin`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pin }),
  })
  if (res.status === 401) {
    throw new Error('INVALID_PIN')
  }
  if (res.status === 429) {
    throw new Error('TOO_MANY_ATTEMPTS')
  }
  if (!res.ok) {
    throw new Error(`POST /api/auth/admin failed: ${res.status}`)
  }
  return res.json() as Promise<AuthResponse>
}

async function fetchBoardPostPage(
  path: string,
  params: { offset?: number; limit?: number },
): Promise<{ posts: BoardPost[]; total: number }> {
  const query = new URLSearchParams()
  if (params.offset !== undefined) query.set('offset', String(params.offset))
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  const qs = query.toString()
  const res = await fetch(`${API_BASE}${path}${qs ? `?${qs}` : ''}`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status}`)
  }
  return (await res.json()) as { posts: BoardPost[]; total: number }
}

export const getBoardPosts = (params: { offset?: number; limit?: number } = {}) =>
  fetchBoardPostPage('/api/board/posts', params)
export const getMyBoardPosts = (params: { offset?: number; limit?: number } = {}) =>
  fetchBoardPostPage('/api/board/posts/mine', params)
export const getLikedBoardPosts = (params: { offset?: number; limit?: number } = {}) =>
  fetchBoardPostPage('/api/board/posts/liked', params)

export async function getBoardPost(postId: string): Promise<BoardPost> {
  const res = await fetch(`${API_BASE}/api/board/posts/${postId}`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`GET /api/board/posts/${postId} failed: ${res.status}`)
  }
  const { post } = (await res.json()) as { post: BoardPost }
  return post
}

export async function createBoardPost(input: {
  title: string
  body: string
  songRequestUrl?: string
}): Promise<BoardPost> {
  const res = await fetch(`${API_BASE}/api/board/posts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getAccessToken() ?? ''}` },
    body: JSON.stringify({
      title: input.title,
      body: input.body,
      songRequestUrl: input.songRequestUrl || null,
    }),
  })
  if (!res.ok) {
    throw new Error(`POST /api/board/posts failed: ${res.status}`)
  }
  const { post } = (await res.json()) as { post: BoardPost }
  return post
}

export async function getComments(postId: string, params: {
  offset?: number
  limit?: number
} = {}): Promise<{ comments: BoardComment[]; total: number }> {
  const query = new URLSearchParams()
  if (params.offset !== undefined) query.set('offset', String(params.offset))
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  const qs = query.toString()
  const res = await fetch(`${API_BASE}/api/board/posts/${postId}/comments${qs ? `?${qs}` : ''}`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`GET /api/board/posts/${postId}/comments failed: ${res.status}`)
  }
  return (await res.json()) as { comments: BoardComment[]; total: number }
}

export async function createComment(postId: string, body: string): Promise<BoardComment> {
  const res = await fetch(`${API_BASE}/api/board/posts/${postId}/comments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getAccessToken() ?? ''}` },
    body: JSON.stringify({ body }),
  })
  if (!res.ok) {
    throw new Error(`POST /api/board/posts/${postId}/comments failed: ${res.status}`)
  }
  const { comment } = (await res.json()) as { comment: BoardComment }
  return comment
}

async function toggleLike(url: string, method: 'POST' | 'DELETE'): Promise<LikeStatus> {
  const res = await fetch(url, {
    method,
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`${method} ${url} failed: ${res.status}`)
  }
  return (await res.json()) as LikeStatus
}

export const likePost = (postId: string) => toggleLike(`${API_BASE}/api/board/posts/${postId}/like`, 'POST')
export const unlikePost = (postId: string) => toggleLike(`${API_BASE}/api/board/posts/${postId}/like`, 'DELETE')
export const likeComment = (commentId: string) =>
  toggleLike(`${API_BASE}/api/board/comments/${commentId}/like`, 'POST')
export const unlikeComment = (commentId: string) =>
  toggleLike(`${API_BASE}/api/board/comments/${commentId}/like`, 'DELETE')

export async function deleteBoardPost(postId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/board/posts/${postId}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`DELETE /api/board/posts/${postId} failed: ${res.status}`)
  }
}

export async function deleteComment(postId: string, commentId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/board/posts/${postId}/comments/${commentId}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`DELETE /api/board/posts/${postId}/comments/${commentId} failed: ${res.status}`)
  }
}

async function fetchReplayPage(
  path: string,
  params: { offset?: number; limit?: number },
): Promise<{ replays: ReplaySession[]; total: number }> {
  const query = new URLSearchParams()
  if (params.offset !== undefined) query.set('offset', String(params.offset))
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  const qs = query.toString()
  const res = await fetch(`${API_BASE}${path}${qs ? `?${qs}` : ''}`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status}`)
  }
  return (await res.json()) as { replays: ReplaySession[]; total: number }
}

// 다중 방송: 내가 진행한 방송의 다시듣기를 모든 채널에서 모아 최신순으로
export const getMyReplays = (params: { offset?: number; limit?: number } = {}) =>
  fetchReplayPage('/api/replays/mine', params)
export const getBookmarkedReplays = (params: { offset?: number; limit?: number } = {}) =>
  fetchReplayPage('/api/replays/bookmarked', params)

async function toggleBookmark(sessionId: string, method: 'POST' | 'DELETE'): Promise<{
  bookmarkCount: number
  bookmarked: boolean
}> {
  const res = await fetch(`${API_BASE}/api/replays/${sessionId}/bookmark`, {
    method,
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: string } | null
    throw new Error(body?.detail || `${method} /api/replays/${sessionId}/bookmark failed: ${res.status}`)
  }
  return (await res.json()) as { bookmarkCount: number; bookmarked: boolean }
}

export const bookmarkReplay = (sessionId: string) => toggleBookmark(sessionId, 'POST')
export const unbookmarkReplay = (sessionId: string) => toggleBookmark(sessionId, 'DELETE')

export async function deleteReplay(sessionId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/replays/${sessionId}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: string } | null
    throw new Error(body?.detail || `DELETE /api/replays/${sessionId} failed: ${res.status}`)
  }
}

export async function getNotifications(params: {
  offset?: number
  limit?: number
} = {}): Promise<{ notifications: NotificationItem[]; total: number; unreadCount: number }> {
  const query = new URLSearchParams()
  if (params.offset !== undefined) query.set('offset', String(params.offset))
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  const qs = query.toString()
  const res = await fetch(`${API_BASE}/api/notifications${qs ? `?${qs}` : ''}`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`GET /api/notifications failed: ${res.status}`)
  }
  return (await res.json()) as { notifications: NotificationItem[]; total: number; unreadCount: number }
}

export async function markNotificationRead(notificationId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/notifications/${notificationId}/read`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`POST /api/notifications/${notificationId}/read failed: ${res.status}`)
  }
}

export async function markAllNotificationsRead(): Promise<void> {
  const res = await fetch(`${API_BASE}/api/notifications/read-all`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`POST /api/notifications/read-all failed: ${res.status}`)
  }
}

export async function deleteNotification(notificationId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/notifications/${notificationId}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`DELETE /api/notifications/${notificationId} failed: ${res.status}`)
  }
}

export async function deleteAllNotifications(): Promise<void> {
  const res = await fetch(`${API_BASE}/api/notifications`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`DELETE /api/notifications failed: ${res.status}`)
  }
}
