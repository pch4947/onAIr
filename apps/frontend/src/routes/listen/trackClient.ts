export interface TrackCandidate {
  video_id: string
  url: string
  title: string
  channel: string
  duration_ms: number
  thumbnail_url: string
}

export function normalizeYoutubeLink(value: string): string | null {
  try {
    const url = new URL(value.trim())
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return null
    let id = ''
    if (url.hostname === 'youtu.be') id = url.pathname.slice(1)
    else if (['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'].includes(url.hostname)) {
      if (url.pathname === '/watch' && url.searchParams.getAll('v').length === 1) id = url.searchParams.get('v') ?? ''
      else if (/^\/(shorts|embed)\/[^/]+$/.test(url.pathname)) id = url.pathname.split('/').at(-1) ?? ''
    }
    return /^[\w-]{11}$/.test(id) ? `https://www.youtube.com/watch?v=${id}` : null
  } catch { return null }
}

export function youtubeThumbnailFromRef(trackRef: string): string | null {
  const videoId = trackRef.startsWith('yt:') ? trackRef.slice(3) : null
  return videoId ? `https://i.ytimg.com/vi/${videoId}/mqdefault.jpg` : null
}

const errors: Record<number, string> = {
  401: '로그인 후 다시 시도해주세요.',
  403: '음악을 신청할 권한이 없어요.',
  404: '방송을 찾을 수 없어요.',
  409: '방송 시작 상태를 확인해주세요. 이미 사용한 요청이라면 창을 다시 열어주세요.',
  422: '올바른 유튜브 영상 링크를 입력해주세요.',
  429: '다른 음원을 준비하고 있어요. 잠시 후 다시 시도해주세요.',
  502: '영상 다운로드 또는 변환에 실패했어요. 접근 가능한 일반 영상을 사용해주세요.',
  503: '음원 서비스를 사용할 수 없어요. 잠시 후 다시 시도해주세요.',
  504: '처리 시간이 초과됐어요. 같은 링크로 다시 시도해주세요.',
}

export function createTrackClient(base: string, getToken: () => string | null) {
  async function call(path: string, options: RequestInit = {}) {
    const token = getToken()
    if (!token) throw new Error(errors[401])
    let response: Response
    try {
      response = await fetch(`${base}/api/stations/${path}`, {
        ...options, headers: { ...options.headers, Authorization: `Bearer ${token}` },
        signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(260000),
      })
    } catch { throw new Error('연결이 끊겼거나 시간이 초과됐어요. 같은 링크로 다시 시도해주세요.') }
    if (!response.ok) throw new Error(errors[response.status] ?? '음악 등록에 실패했어요. 다시 시도해주세요.')
    return response.json()
  }
  return {
    async search(stationId: string, query: string, signal?: AbortSignal): Promise<{ candidates: TrackCandidate[] }> {
      return call(`${encodeURIComponent(stationId)}/tracks/search`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query }), signal,
      })
    },
    async checkStation(stationId: string): Promise<boolean> {
      const result = await call(`${encodeURIComponent(stationId)}/tracks/access`)
      return result.can_request === true
    },
    async enqueue(stationId: string, url: string, key: string): Promise<{
      track: { title: string; artist: string; duration_ms: number; track_ref: string }
    }> {
      return call(`${encodeURIComponent(stationId)}/tracks`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
        body: JSON.stringify({ url, source: 'request' }),
      })
    },
  }
}
