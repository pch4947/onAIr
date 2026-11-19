// 계정 설정 API (#325) — 일반 회원 계정만 쓴다. 게스트·어드민은 서버가 403으로 막는다
import { getAccessToken } from '@/shared/authToken'
import type { AuthUser } from '@/shared/types'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

// 화면 문구를 고를 수 있게 상태 코드와 서버가 준 detail을 그대로 싣는다
export class AccountError extends Error {
  readonly status: number
  readonly detail: unknown
  constructor(status: number, detail: unknown) {
    super(`ACCOUNT_${status}`)
    this.status = status
    this.detail = detail
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}/api/account${path}`, {
    method,
    headers: { Authorization: `Bearer ${getAccessToken() ?? ''}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const payload = await res.json().catch(() => null) as { detail?: unknown } | null
    throw new AccountError(res.status, payload?.detail)
  }
  return (res.status === 204 ? undefined : await res.json()) as T
}

export async function changeName(name: string): Promise<AuthUser> {
  return (await call<{ user: AuthUser }>('POST', '/name', { name })).user
}

export function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  return call('POST', '/password', { currentPassword, newPassword })
}

export async function updatePreferences(defaultAnonymous: boolean): Promise<AuthUser> {
  return (await call<{ user: AuthUser }>('POST', '/preferences', { defaultAnonymous })).user
}

export function deleteAccount(password: string): Promise<void> {
  return call('DELETE', '', { password })
}
