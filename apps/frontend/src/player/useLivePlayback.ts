import { useCallback, useEffect, useRef, useState } from 'react'
import Hls from 'hls.js'

export function useLivePlayback(url: string | null) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const hlsRef = useRef<Hls | null>(null)
  const [isPlaying, setIsPlaying] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !url) return
    let timer: ReturnType<typeof setTimeout> | undefined
    let retries = 0
    const onPlay = () => { setIsPlaying(true); setError(null) }
    const onPause = () => setIsPlaying(false)
    const onError = () => setError('방송 연결을 확인한 뒤 다시 재생해 주세요.')
    audio.addEventListener('playing', onPlay)
    audio.addEventListener('pause', onPause)
    audio.addEventListener('error', onError)
    if (Hls.isSupported()) {
      const hls = new Hls({ liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 6 })
      hlsRef.current = hls
      hls.attachMedia(audio)
      hls.loadSource(url)
      hls.on(Hls.Events.ERROR, (_, data) => {
        if (!data.fatal) return
        onError()
        if (retries++ < 5) {
          clearTimeout(timer)
          timer = setTimeout(() => {
            if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError()
            else hls.loadSource(url)
          }, 2000)
        }
      })
    } else if (audio.canPlayType('application/vnd.apple.mpegurl')) {
      audio.src = url
    } else {
      onError()
    }
    return () => {
      clearTimeout(timer)
      hlsRef.current?.destroy()
      hlsRef.current = null
      audio.pause()
      audio.removeAttribute('src')
      audio.load()
      audio.removeEventListener('playing', onPlay)
      audio.removeEventListener('pause', onPause)
      audio.removeEventListener('error', onError)
    }
  }, [url])

  const togglePlay = useCallback(async () => {
    const audio = audioRef.current
    if (!audio || !url) return
    if (!audio.paused) { audio.pause(); return }
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
