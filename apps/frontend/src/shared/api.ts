import type { BroadcastStateResponse, SubmitRequestResponse } from '@/shared/types'

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
