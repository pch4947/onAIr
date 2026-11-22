import { useCallback, useEffect, useRef, useState } from 'react'
import Hls from 'hls.js'

export function useLivePlayback(url: string | null, { autoplay = true }: { autoplay?: boolean } = {}) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const hlsRef = useRef<Hls | null>(null)
  // 재시도를 다 쓴 뒤 재생 버튼으로 다시 연결한다 — 연결이 살아 있으면 false
  const reconnectRef = useRef<(() => boolean) | null>(null)
  const [isPlaying, setIsPlaying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 설정을 바꿔도 연결을 다시 맺지 않는다 — 다음에 방송을 열 때부터 적용된다
  const autoplayRef = useRef(autoplay)
  useEffect(() => { autoplayRef.current = autoplay }, [autoplay])

  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !url) return
    setError(null)
    let timer: ReturnType<typeof setTimeout> | undefined
    // 연달아 실패한 횟수 — 다시 재생되면 0으로 돌린다. 쌓이기만 하면 긴 방송 중 끊김 다섯 번 뒤로는 다시 잇지 않는다 (#366)
    let retries = 0
    let stopped = false
    let firstManifest = true
    let resume = false
    const onPlay = () => { setIsPlaying(true); setError(null); retries = 0; stopped = false }
    const onPause = () => setIsPlaying(false)
    const onError = () => setError('방송 연결을 확인한 뒤 다시 재생해 주세요.')
    audio.addEventListener('playing', onPlay)
    audio.addEventListener('pause', onPause)
    audio.addEventListener('error', onError)
    // 브라우저 autoplay 정책으로 거부될 수 있어 실패는 무시하고 기존 재생 버튼으로 폴백한다.
    // 자동 재생을 끈 설정(#324)이면 재생 버튼을 누를 때까지 기다린다.
    const attemptAutoplay = () => { if (autoplayRef.current) audio.play().catch(() => {}) }
    if (Hls.isSupported()) {
      const hls = new Hls({ liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 6 })
      hlsRef.current = hls
      hls.attachMedia(audio)
      hls.loadSource(url)
      // 처음 연결할 때만 설정대로 자동 재생한다. 다시 연결할 때는 끊기기 전에 듣고 있었을 때만 이어서 튼다 —
      // 멈춰 둔 방송이 연결 오류 뒤에 저절로 다시 나오면 안 된다
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        if (firstManifest) {
          firstManifest = false
          attemptAutoplay()
        } else if (resume) {
          audio.play().catch(() => {})
        }
      })
      hls.on(Hls.Events.ERROR, (_, data) => {
        if (!data.fatal) return
        onError()
        if (retries++ >= 5) {
          stopped = true
          return
        }
        const wasPlaying = !audio.paused
        clearTimeout(timer)
        timer = setTimeout(() => {
          if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            hls.recoverMediaError()
          } else {
            resume = wasPlaying
            hls.loadSource(url)
          }
        }, 2000)
      })
      reconnectRef.current = () => {
        if (!stopped) return false
        retries = 0
        stopped = false
        resume = true
        hls.loadSource(url)
        return true
      }
    } else if (audio.canPlayType('application/vnd.apple.mpegurl')) {
      audio.src = url
      attemptAutoplay()
    } else {
      onError()
    }
    return () => {
      clearTimeout(timer)
      reconnectRef.current = null
      hlsRef.current?.destroy()
      hlsRef.current = null
      audio.pause()
      audio.removeAttribute('src')
      audio.load()
      audio.removeEventListener('playing', onPlay)
      audio.removeEventListener('pause', onPause)
      audio.removeEventListener('error', onError)
      // pause 이벤트는 비동기로 와서 위에서 지운 리스너가 받지 못한다 — 방송이 바뀌거나 끝났는데 "재생 중"으로 남는다 (#366)
      setIsPlaying(false)
    }
  }, [url])

  const togglePlay = useCallback(async () => {
    const audio = audioRef.current
    if (!audio || !url) return
    if (!audio.paused) { audio.pause(); return }
    if (reconnectRef.current?.()) return
    const live = hlsRef.current?.liveSyncPosition
    if (live != null) audio.currentTime = live
    else if (audio.seekable.length) audio.currentTime = Math.max(audio.seekable.start(0), audio.seekable.end(audio.seekable.length - 1) - 12)
    try {
      await audio.play()
    } catch {
      setError('재생하지 못했습니다. 잠시 후 재생 버튼을 다시 눌러 주세요.')
    }
  }, [url])

  return { audioRef, isPlaying, error, togglePlay }
}
