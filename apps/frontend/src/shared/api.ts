import { getAccessToken } from '@/shared/authToken'
import type {
  AuthResponse,
  AuthUser,
  BoardComment,
  BoardPost,
  BroadcastStateResponse,
  LikeStatus,
  NotificationItem,
  ReplaySession,
  RequestRecord,
  SubmitRequestResponse,
} from '@/shared/types'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

export async function getBroadcastState(): Promise<BroadcastStateResponse> {
  const res = await fetch(`${API_BASE}/api/stream/state`)
  if (!res.ok) {
    throw new Error(`GET /api/stream/state failed: ${res.status}`)
  }
  return res.json() as Promise<BroadcastStateResponse>
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
  prompt: string
  listenerId?: string
  kind?: 'story' | 'mood'
}): Promise<SubmitRequestResponse> {
  const res = await fetch(`${API_BASE}/api/requests`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt: input.prompt, listenerId: input.listenerId, kind: input.kind }),
  })
  if (!res.ok) {
    throw new Error(`POST /api/requests failed: ${res.status}`)
  }
  return res.json() as Promise<SubmitRequestResponse>
}

export async function getRequests(): Promise<{ requests: RequestRecord[] }> {
  const res = await fetch(`${API_BASE}/api/requests`)
  if (!res.ok) {
    throw new Error(`GET /api/requests failed: ${res.status}`)
  }
  return res.json() as Promise<{ requests: RequestRecord[] }>
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

export const getMyStationReplays = (stationId: string, params: { offset?: number; limit?: number } = {}) =>
  fetchReplayPage(`/api/stations/${stationId}/replays/mine`, params)
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
