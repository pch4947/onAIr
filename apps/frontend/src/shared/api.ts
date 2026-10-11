import { getAccessToken } from '@/shared/authToken'
import type {
  AuthResponse,
  AuthUser,
  BoardComment,
  BoardPost,
  BroadcastStateResponse,
  LikeStatus,
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

export async function submitRequest(prompt: string): Promise<SubmitRequestResponse> {
  const res = await fetch(`${API_BASE}/api/requests`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt }),
  })
  if (!res.ok) {
    throw new Error(`POST /api/requests failed: ${res.status}`)
  }
  return res.json() as Promise<SubmitRequestResponse>
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

export async function getBoardPosts(params: {
  offset?: number
  limit?: number
} = {}): Promise<{ posts: BoardPost[]; total: number }> {
  const query = new URLSearchParams()
  if (params.offset !== undefined) query.set('offset', String(params.offset))
  if (params.limit !== undefined) query.set('limit', String(params.limit))
  const qs = query.toString()
  const res = await fetch(`${API_BASE}/api/board/posts${qs ? `?${qs}` : ''}`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}` },
  })
  if (!res.ok) {
    throw new Error(`GET /api/board/posts failed: ${res.status}`)
  }
  return (await res.json()) as { posts: BoardPost[]; total: number }
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
