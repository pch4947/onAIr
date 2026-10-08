import { getAccessToken } from '@/shared/authToken'
import type { AuthResponse, AuthUser, BroadcastStateResponse, SubmitRequestResponse } from '@/shared/types'

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
