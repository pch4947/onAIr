import { useEffect, useRef, useState } from 'react'
import Hls from 'hls.js'

// useLivePlayback의 축소판(VOD용): 라이브 싱크/재시도 로직 없음 —
// 재생 버튼 클릭 직후 바로 재생을 시도하고, 이후 일시정지/탐색은 <audio controls>의 네이티브 UI에 맡김.
export function useVodPlayback(url: string | null) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const hlsRef = useRef<Hls | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !url) return
    setError(null)
    const onMediaError = () => setError('다시듣기를 불러오지 못했어요')
    audio.addEventListener('error', onMediaError)
    const attemptPlay = () => { audio.play().catch(() => {}) }
    if (Hls.isSupported()) {
      const hls = new Hls()
      hlsRef.current = hls
      hls.attachMedia(audio)
      hls.loadSource(url)
      hls.on(Hls.Events.MANIFEST_PARSED, attemptPlay)
      hls.on(Hls.Events.ERROR, (_, data) => {
        if (data.fatal) setError('다시듣기를 불러오지 못했어요')
      })
    } else if (audio.canPlayType('application/vnd.apple.mpegurl')) {
      audio.src = url
      attemptPlay()
    } else {
      setError('이 브라우저에서는 다시듣기를 재생할 수 없어요')
    }
    return () => {
      audio.removeEventListener('error', onMediaError)
      hlsRef.current?.destroy()
      hlsRef.current = null
      audio.pause()
      audio.removeAttribute('src')
      audio.load()
    }
  }, [url])

  return { audioRef, error }
}
