import type { Persona, PersonaFieldError, PersonaForm, StationInfo, Voice } from '../../shared/types'

type WirePersona = Omit<Persona, 'musicTaste' | 'djName' | 'signaturePhrases'> & {
  music_taste: string[]; dj_name: string; signature_phrases: string[]
}

export interface StationResult {
  station_id: string
  status: 'preparing' | 'started' | 'rejected' | 'draining' | 'stopping' | 'cleaning' | 'ended'
  created_at: number
  hls_ready: boolean
  hls_url: string
  engine_result: { reason?: string; detail?: string } | null
}

export class StationApiError extends Error {
  status: number
  errors: PersonaFieldError[]
  constructor(status: number, errors: PersonaFieldError[] = []) {
    super(status === 401 ? '로그인이 필요해요. 다시 로그인해주세요.'
      : status === 409 ? '이미 생성된 방송이 있어요. 현재는 방송 하나만 만들 수 있어요.'
      : status === 422 ? '입력 내용을 확인해주세요.'
      : '요청을 완료하지 못했어요. 잠시 후 다시 시도해주세요.')
    this.status = status
    this.errors = errors
  }
}

function toWire(persona: Persona): WirePersona {
  const { musicTaste, djName, signaturePhrases, ...rest } = persona
  return { ...rest, music_taste: musicTaste, dj_name: djName, signature_phrases: signaturePhrases }
}

function fromWire(persona: WirePersona): Persona {
  const { music_taste, dj_name, signature_phrases, ...rest } = persona
  return { ...rest, musicTaste: music_taste, djName: dj_name, signaturePhrases: signature_phrases }
}

export function createStationClient(base: string, getToken: () => string | null) {
  async function request<T>(path: string, body?: unknown, key?: string, signal?: AbortSignal): Promise<T> {
    const token = getToken()
    if (!token) throw new StationApiError(401)
    let response: Response
    try {
      response = await fetch(`${base}/api${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json',
          ...(key ? { 'Idempotency-Key': key } : {}) },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(35000)]) : AbortSignal.timeout(35000),
      })
    } catch (error) {
      if (signal?.aborted) throw error
      throw new StationApiError(503)
    }
    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      // Also tolerate the older backend's detail.errors while it is being deployed.
      const errors = payload?.errors ?? payload?.detail?.errors ??
        (Array.isArray(payload?.detail) ? payload.detail.map((item: { loc: (string | number)[]; msg: string }) => ({
          field: item.loc.filter((part) => part !== 'body').join('.'), reason: item.msg,
        })) : [])
      throw new StationApiError(response.status, errors)
    }
    if (payload === null) throw new StationApiError(502)
    return payload as T
  }

  return {
    async getVoices(): Promise<Voice[]> {
      return (await request<{ voices: Voice[] }>('/voices')).voices
    },
    async generatePersonaDrafts(form: PersonaForm, count = 3): Promise<Persona[]> {
      const result = await request<{ drafts: WirePersona[] }>('/personas/drafts', { count, form: {
        ...form.style, voice: form.voice, music_taste: form.musicTaste,
        dj_name: form.djName || null, host_note: form.hostNote || null,
      } })
      return result.drafts.map(fromWire)
    },
    async checkPersonaDraft(persona: Persona): Promise<{ ok: true } | { ok: false; errors: PersonaFieldError[] }> {
      try {
        return await request<{ ok: true }>('/personas/check', { persona: toWire(persona) })
      } catch (error) {
        if (error instanceof StationApiError && error.status === 422) return { ok: false, errors: error.errors }
        throw error
      }
    },
    async createStation(input: StationInfo & { persona: Persona }, key: string): Promise<StationResult> {
      if (input.firstSongUrl) throw new Error('첫 노래 지정은 아직 지원하지 않아요.')
      return (await request<{ station: StationResult }>('/stations', {
        persona: toWire(input.persona), broadcast_minutes: input.broadcastMinutes, topic: input.topic || null,
        format: input.format, intensity: input.intensity,
        ...(input.format === 'talk' && input.corners ? { corners: input.corners } : {}),
        ...(input.format === 'news' && input.newsSections ? { news_sections: input.newsSections } : {}),
      }, key)).station
    },
    async getStation(id: string, signal?: AbortSignal): Promise<StationResult> {
      return (await request<{ station: StationResult }>(`/stations/${encodeURIComponent(id)}`, undefined, undefined, signal)).station
    },
    async stopStation(id: string, createdAt: number): Promise<StationResult> {
      return (await request<{ station: StationResult }>(`/stations/${encodeURIComponent(id)}/stop`,
        { created_at: createdAt })).station
    },
  }
}
